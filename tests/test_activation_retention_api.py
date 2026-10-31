from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import threading
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


def test_activation_status_is_read_only_and_start_is_idempotent(activation_client, conn):
    first_status = activation_client.get("/api/activation/status")
    second_status = activation_client.get("/api/activation/status")
    assert first_status.status_code == 200
    assert second_status.status_code == 200
    assert first_status.json()["model_mode"] == "byok"
    assert first_status.json()["model_ready"] is False
    assert first_status.json()["cohort_started_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM activation_cohort").fetchone()[0] == 0

    started = activation_client.post("/api/activation/start")
    repeated = activation_client.post("/api/activation/start")
    assert started.status_code == 200
    assert repeated.status_code == 200
    assert started.json()["first_seen_at"] == repeated.json()["first_seen_at"]
    assert conn.execute("SELECT COUNT(*) FROM activation_cohort").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM content_events WHERE event_type='activation_started'"
    ).fetchone()[0] == 1


def test_activation_cohort_is_independent_per_tenant(activation_client, conn):
    first = activation_client.post("/api/activation/start")
    assert first.status_code == 200
    main._g_ctx["runtime_tenant_id"] = "tenant-b"
    # 模拟另一个已刷新上下文的客户端；旧客户端仍携带 default，应被后端拒绝。
    activation_client.headers.update({"X-Tenant-Id": "tenant-b"})
    second = activation_client.post("/api/activation/start")
    assert second.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM activation_cohort").fetchone()[0] == 2
    rows = conn.execute(
        "SELECT tenant_id, first_seen_at FROM activation_cohort ORDER BY tenant_id"
    ).fetchall()
    assert [row["tenant_id"] for row in rows] == ["default", "tenant-b"]
    assert all(row["first_seen_at"] for row in rows)


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


def test_activation_rejects_identical_outputs_across_angles(activation_client, conn, monkeypatch):
    cfg = main._g_ctx["cfg"]
    cfg.llm.enabled = True
    monkeypatch.setenv(cfg.llm.api_key_env, "test-only-key")
    from src.core import content_pipeline
    calls = {"count": 0}
    body = (
        "这是一段用于验证重复内容检测的中文正文，内容包含完整的背景、具体步骤和实际建议。"
        "用户可以先明确自己的目标，再把任务拆分成容易执行的小步骤，并记录每一步的结果。"
        "完成之后应当检查效果，根据反馈调整方法，而不是机械地重复相同操作。"
        "整个过程需要结合真实场景，补充个人判断和有用细节，避免生成看起来不同但实际相同的内容。"
    )
    def identical(*args, **kwargs):
        calls["count"] += 1
        return ("完全相同的体验样稿标题", body, ["实践", "方法"])
    monkeypatch.setattr(content_pipeline, "generate_copy_content", identical)
    response = activation_client.post("/api/activation/samples", json={
        "account_description": "分享普通上班族的实用效率方法",
        "platform": "xhs", "idempotency_key": "duplicate-across-angles",
    })
    run_id = response.json()["sample_run_id"]
    result = None
    for _ in range(100):
        result = activation_client.get(f"/api/activation/samples/{run_id}").json()
        if result["run"]["status"] in ("ready", "failed"):
            break
        time.sleep(0.02)
    assert result["run"]["status"] == "failed"
    assert result["run"]["error_code"] == "quality_rejected"
    assert calls["count"] > 3


def test_activation_sample_success_stays_outside_formal_production(activation_client, conn, monkeypatch):
    cfg = main._g_ctx["cfg"]
    cfg.llm.enabled = True
    monkeypatch.setenv(cfg.llm.api_key_env, "test-only-key")
    from src.core import content_pipeline

    base_body = (
        "个人成长 实用经验 产品经理 AI工具 需求分析 用户研究 项目管理 工作效率 数据分析 "
        "业务流程 产品设计 用户反馈 方案验证 目标拆解 方法总结 实践案例 场景分析 效率提升 团队协作 持续迭代 经验复盘"
    )
    variations = [
        "第一篇重点讲如何把复杂需求拆成可验证的小任务，并通过访谈和数据确认优先级。",
        "第二篇重点讲如何建立团队协作节奏，明确责任边界，并减少重复沟通和返工。",
        "第三篇重点讲如何复盘项目结果，把有效经验沉淀成模板并持续优化执行流程。",
    ]
    calls = {"count": 0}
    def varied(*args, **kwargs):
        index = calls["count"] % 3
        calls["count"] += 1
        return (f"产品经理的实用方法 {index}", base_body + " " + variations[index], ["产品经理", "实用经验"])
    monkeypatch.setattr(content_pipeline, "generate_copy_content", varied)
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
    from datetime import datetime
    conn = seed.conn
    conn.execute("UPDATE drafts SET user_confirmed_at=? WHERE id=?",
                 (datetime.now().isoformat(sep=" "), draft_id))
    conn.commit()
    unsupported = activation_client.post(
        f"/api/drafts/{draft_id}/metrics/manual",
        json={"views": 20, "collects": 3},
    )
    assert unsupported.status_code == 422
    accepted = activation_client.post(
        f"/api/drafts/{draft_id}/metrics/manual",
        json={"views": 20, "likes": 2, "comments": 1},
    )
    assert accepted.status_code == 200, accepted.text


def test_sample_delete_hides_history_but_preserves_quota_audit_and_saved_content(activation_client, conn):
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
    assert conn.execute("SELECT deleted_at FROM activation_run_deletions WHERE sample_run_id='delete-run'").fetchone()[0] is not None
    assert conn.execute("SELECT COUNT(*) FROM sample_contents WHERE id='delete-sample'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM content_events WHERE id IN ('delete-start','delete-save')").fetchone()[0] == 2


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
        json={"views": 128, "likes": 12, "collects": None},
    )
    assert metrics.status_code == 200, metrics.text
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
        conn.execute("UPDATE drafts SET user_confirmed_at=? WHERE id=?",
                     (now, draft_id))
        conn.commit()
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


def test_weekly_action_feedback_persists_decisions_and_outcomes(activation_client, conn, seed):
    from datetime import datetime

    now = datetime.now().isoformat(sep=" ")
    round_id = seed.round()
    for i, views in enumerate((100, 200, 300), start=1):
        draft_id = seed.draft(
            round_id, platform="xhs", draft_status="published",
            fetch_status=None, topic_id=f"weekly-feedback-{i}",
        )
        conn.execute("UPDATE drafts SET user_confirmed_at=? WHERE id=?", (now, draft_id))
        conn.commit()
        response = activation_client.post(
            f"/api/drafts/{draft_id}/metrics/manual",
            json={"views": views, "measured_at": now},
        )
        assert response.status_code == 200, response.text

    report = activation_client.get("/api/reports/weekly").json()
    assert report["next_actions"]
    action = report["next_actions"][0]
    assert len(action["suggestion_key"]) == 64
    assert action["decision"] == "pending"
    assert action["outcome"] == "pending"

    adopted = activation_client.post(
        "/api/reports/weekly/actions",
        json={"suggestion_key": action["suggestion_key"], "decision": "adopted"},
    )
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["decision"] == "adopted"

    refreshed = activation_client.get("/api/reports/weekly").json()
    persisted = next(item for item in refreshed["next_actions"]
                     if item["suggestion_key"] == action["suggestion_key"])
    assert persisted["decision"] == "adopted"
    assert persisted["outcome"] == "pending"

    outcome = activation_client.post(
        "/api/reports/weekly/actions",
        json={
            "suggestion_key": action["suggestion_key"],
            "decision": "adopted",
            "outcome": "success",
            "outcome_note": "对照测试达到预期",
        },
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["outcome"] == "success"
    assert outcome.json()["outcome_note"] == "对照测试达到预期"

    final_report = activation_client.get("/api/reports/weekly").json()
    final_action = next(item for item in final_report["next_actions"]
                        if item["suggestion_key"] == action["suggestion_key"])
    assert final_action["decision"] == "adopted"
    assert final_action["outcome"] == "success"


def test_saved_sample_promotes_into_created_formal_round_idempotently(activation_client, conn, seed):
    from datetime import datetime
    from types import SimpleNamespace

    main._g_ctx["guard"] = SimpleNamespace(
        check_content=lambda *args, **kwargs: SimpleNamespace(passes=True, reason="通过正式质量门")
    )
    now = datetime.now().isoformat(sep=" ")
    round_id = seed.round(status="created")
    conn.execute(
        "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, model_mode, "
        "idempotency_key, created_at, completed_at) VALUES ('sample-run-promote','default','xhs',"
        "'分享产品经理的实用方法','ready','byok','promote-key',?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO sample_contents (id, sample_run_id, tenant_id, platform, angle, title, body, "
        "hashtags_json, quality_status, adopted_at, created_at, updated_at) "
        "VALUES ('sample-content-promote','sample-run-promote','default','xhs','实用方法',"
        "'产品经理的实用方法','产品经理可以通过需求拆解、用户访谈和数据复盘提升工作效率。',"
        "'[]','pass',?,?,?)",
        (now, now, now),
    )
    conn.commit()

    response = activation_client.post(
        "/api/activation/saved/sample-content-promote/promote",
        json={"round_id": round_id},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["round_id"] == round_id
    assert payload["already_promoted"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM contents WHERE id=? AND round_id=?",
        (payload["content_id"], round_id),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT status FROM drafts WHERE id=?", (payload["draft_id"],)
    ).fetchone()[0] == "draft_pending"

    repeated = activation_client.post(
        "/api/activation/saved/sample-content-promote/promote",
        json={"round_id": round_id},
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["already_promoted"] is True
    assert repeated.json()["content_id"] == payload["content_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM contents WHERE round_id=?", (round_id,)
    ).fetchone()[0] == 1


def test_activation_metrics_reports_ttv_percentiles_for_available_database_cohorts(activation_client, conn):
    from datetime import datetime, timedelta

    now = datetime.now()
    cohorts = [
        ("default", now - timedelta(minutes=10), 120),
        ("tenant-b", now - timedelta(minutes=20), 300),
    ]
    for tenant_id, started, duration in cohorts:
        first_seen = started.isoformat(sep=" ")
        created = (started + timedelta(seconds=10)).isoformat(sep=" ")
        model_started = (started + timedelta(seconds=20)).isoformat(sep=" ")
        completed = (started + timedelta(seconds=duration)).isoformat(sep=" ")
        conn.execute(
            "INSERT INTO activation_cohort (tenant_id, first_seen_at) VALUES (?, ?)",
            (tenant_id, first_seen),
        )
        conn.execute(
            "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, model_mode, "
            "idempotency_key, created_at, started_at, completed_at) "
            "VALUES (?, ?, 'xhs', '测试账号定位内容足够长', 'ready', 'byok', ?, ?, ?, ?)",
            (f"ttv-{tenant_id}", tenant_id, f"ttv-{tenant_id}", created, model_started, completed),
        )
    conn.commit()

    response = activation_client.get("/api/activation/metrics")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cohort_size"] == 1
    assert payload["ttv_sample_count"] == 1
    assert payload["ttv_p50_seconds"] is None
    assert payload["ttv_p90_seconds"] is None
    assert "至少 5 个" in payload["ttv_percentile_scope"]


def test_activation_metrics_calculates_completed_cohort_rates_and_ttv(activation_client, conn, seed):
    from datetime import datetime, timedelta
    started = datetime.now() - timedelta(days=8)
    completed = started + timedelta(seconds=120)
    now = datetime.now()
    conn.execute(
        "INSERT INTO activation_cohort (tenant_id, first_seen_at) VALUES ('default', ?)",
        (started.isoformat(sep=" "),),
    )
    conn.execute(
        "INSERT INTO sample_runs "
        "(id, tenant_id, platform, account_description, status, idempotency_key, created_at, started_at, completed_at) "
        "VALUES ('cohort-run', 'default', 'xhs', '分享产品经理实践方法', 'ready', 'cohort-key', ?, ?, ?)",
        (started.isoformat(sep=" "), started.isoformat(sep=" "), completed.isoformat(sep=" ")),
    )
    conn.execute(
        "INSERT INTO sample_runs "
        "(id, tenant_id, platform, account_description, status, idempotency_key, created_at, started_at, completed_at) "
        "VALUES ('cohort-run-failed', 'default', 'xhs', '分享产品经理实践方法', 'failed', 'cohort-failed-key', ?, ?, ?)",
        (started.isoformat(sep=" "), started.isoformat(sep=" "), (started + timedelta(seconds=40)).isoformat(sep=" ")),
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
    # A second published draft with a note URL but no metrics still counts as data returned.
    _linked_draft = seed.draft(_round, platform="xhs", draft_status="published", fetch_status=None, topic_id="linked-only")
    _linked_content = conn.execute("SELECT content_id FROM drafts WHERE id=?", (_linked_draft,)).fetchone()[0]
    _linked_at = (started + timedelta(hours=5)).isoformat(sep=" ")
    conn.execute("UPDATE drafts SET user_confirmed_at=?, platform_note_url=? WHERE id=?",
                 (_linked_at, "https://www.xiaohongshu.com/explore/linked", _linked_draft))
    conn.execute(
        "INSERT INTO content_events (id, tenant_id, content_id, event_type, event_at, source, metadata_json) "
        "VALUES ('cohort-note-url', 'default', ?, 'note_url_added', ?, 'web', '{}')",
        (_linked_content, (started + timedelta(hours=6)).isoformat(sep=" ")),
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
    assert payload["quick_experience_completion_rate"] == 0.5
    assert payload["seven_day_activation_rate"] == 1.0
    assert payload["first_publish_rate"] == 1.0
    assert payload["data_return_rate"] == 1.0


def test_activation_metrics_marks_unfinished_seven_day_window_as_observing(activation_client):
    response = activation_client.get("/api/activation/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["cohort_size"] == 0
    assert payload["seven_day_activation_rate"] is None

def test_chinese_body_passes_character_level_quality_check(activation_client, conn, monkeypatch):
    cfg = main._g_ctx["cfg"]
    cfg.llm.enabled = True
    monkeypatch.setenv(cfg.llm.api_key_env, "test-only-key")
    from src.core import content_pipeline
    body = (
        "很多人做饭时总觉得准备工作很麻烦，其实可以先把一周的食材分成几类。"
        "周末先清洗耐放的蔬菜，再把肉类按每餐分量装袋冷冻。"
        "工作日晚上只需要提前取出一份食材，搭配米饭或面条就能快速完成。"
        "如果早上时间紧张，可以把调味汁提前装进小盒，避免食物浸泡太久影响口感。"
        "刚开始不必追求复杂菜单，先固定三种容易执行的组合，再根据实际情况调整。"
        "这样既能减少临时决定吃什么的压力，也能降低外卖频率。"
    )
    variations = [
        "第一种方法是周末分装食材，按每餐用量冷冻保存，工作日只需简单加热和搭配。",
        "第二种方法是提前准备调味汁和耐放配菜，早上快速组合，避免食材长时间浸泡。",
        "第三种方法是固定三种基础菜单，再根据采购价格和当周安排灵活替换食材。",
    ]
    calls = {"count": 0}
    def varied_chinese(*args, **kwargs):
        index = calls["count"] % 3
        calls["count"] += 1
        return (f"工作日带饭的准备方法{index}", body + variations[index], ["带饭", "效率"])
    monkeypatch.setattr(content_pipeline, "generate_copy_content", varied_chinese)
    response = activation_client.post("/api/activation/samples", json={
        "account_description": "分享普通上班族的健康带饭方法",
        "platform": "xhs", "idempotency_key": "chinese-body-quality",
    })
    run_id = response.json()["sample_run_id"]
    result = None
    for _ in range(100):
        result = activation_client.get(f"/api/activation/samples/{run_id}").json()
        if result["run"]["status"] in ("ready", "failed"):
            break
        time.sleep(0.02)
    assert result["run"]["status"] == "ready", result["run"]
    assert len(result["contents"]) == 3


def test_sensitive_topic_match_does_not_flag_student_party_phrase():
    from src.core.hot_topics import _sensitive_topic_hits
    assert _sensitive_topic_hits("给学生党分享省钱又健康的带饭做法") == []
    assert _sensitive_topic_hits("上班党一周带饭计划") == []
    assert _sensitive_topic_hits("二十届三中全会召开") == ["时政"]
    assert _sensitive_topic_hits("国防部举行新闻发布会") == ["时政"]
    assert _sensitive_topic_hits("台湾旅行攻略") == []
    assert _sensitive_topic_hits("南海海岛旅游计划") == []
    assert _sensitive_topic_hits("台海局势出现变化") == ["涉台及主权"]


def test_activation_saved_library_survives_soft_delete_and_quota_is_not_refunded(activation_client, conn):
    from datetime import datetime
    now = datetime.now().isoformat(sep=" ")
    conn.execute(
        "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, idempotency_key, created_at) "
        "VALUES ('saved-run', 'default', 'xhs', '分享产品经理实践方法', 'ready', 'saved-run-key', ?)", (now,)
    )
    conn.execute(
        "INSERT INTO sample_contents "
        "(id, sample_run_id, tenant_id, platform, angle, title, body, hashtags_json, quality_status, "
        "adopted_at, created_at, updated_at) "
        "VALUES ('saved-content', 'saved-run', 'default', 'xhs', '实用方法', '可找回标题', '可找回正文', '[]', 'pass', ?, ?, ?)",
        (now, now, now),
    )
    conn.commit()
    before = activation_client.get("/api/activation/status").json()["activation_limits"]["daily_runs_used"]
    saved = activation_client.get("/api/activation/saved")
    assert saved.status_code == 200
    assert any(item["id"] == "saved-content" for item in saved.json()["items"])
    deleted = activation_client.delete("/api/activation/samples/saved-run")
    assert deleted.status_code == 200
    after = activation_client.get("/api/activation/status").json()["activation_limits"]["daily_runs_used"]
    assert after == before
    assert conn.execute("SELECT deleted_at FROM activation_run_deletions WHERE sample_run_id='saved-run'").fetchone()[0] is not None
    assert conn.execute("SELECT COUNT(*) FROM content_events WHERE sample_run_id='saved-run'").fetchone()[0] == 0


def test_manual_metrics_rejects_future_and_pre_publish_measurement(activation_client, conn, seed):
    from datetime import datetime, timedelta
    round_id = seed.round()
    draft_id = seed.draft(round_id, platform="xhs", draft_status="published", fetch_status=None)
    confirmed = datetime.now() - timedelta(hours=1)
    conn.execute("UPDATE drafts SET user_confirmed_at=? WHERE id=?",
                 (confirmed.isoformat(sep=" "), draft_id))
    conn.commit()
    before_publish = activation_client.post(
        f"/api/drafts/{draft_id}/metrics/manual",
        json={"views": 10, "measured_at": (confirmed - timedelta(minutes=1)).isoformat(sep=" ")},
    )
    assert before_publish.status_code == 422
    future = activation_client.post(
        f"/api/drafts/{draft_id}/metrics/manual",
        json={"views": 10, "measured_at": (datetime.now() + timedelta(days=30)).isoformat(sep=" ")},
    )
    assert future.status_code == 422
    valid = activation_client.post(
        f"/api/drafts/{draft_id}/metrics/manual",
        json={"views": 10, "measured_at": datetime.now().isoformat(sep=" ")},
    )
    assert valid.status_code == 200


def test_sample_save_and_reject_are_mutually_exclusive(activation_client, conn):
    from datetime import datetime
    now = datetime.now().isoformat(sep=" ")
    conn.execute(
        "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, idempotency_key, created_at) "
        "VALUES ('state-run', 'default', 'xhs', '分享产品经理实践方法', 'ready', 'state-run-key', ?)", (now,)
    )
    conn.execute(
        "INSERT INTO sample_contents "
        "(id, sample_run_id, tenant_id, platform, angle, title, body, hashtags_json, quality_status, created_at, updated_at) "
        "VALUES ('state-content', 'state-run', 'default', 'xhs', '实用方法', '状态标题', '状态正文', '[]', 'pass', ?, ?)",
        (now, now),
    )
    conn.commit()
    base = "/api/activation/samples/state-run/contents/state-content/events"
    rejected = activation_client.post(base, json={"event_type":"rejected","reason":"内容空泛"})
    assert rejected.status_code == 200
    saved = activation_client.post(base, json={"event_type":"saved"})
    assert saved.status_code == 200
    row = conn.execute("SELECT adopted_at, rejected_at, rejection_reason FROM sample_contents WHERE id='state-content'").fetchone()
    assert row["adopted_at"] is not None and row["rejected_at"] is None and row["rejection_reason"] is None
    rejected_again = activation_client.post(base, json={"event_type":"rejected","reason":"定位不符"})
    assert rejected_again.status_code == 200
    row = conn.execute("SELECT adopted_at, rejected_at, rejection_reason FROM sample_contents WHERE id='state-content'").fetchone()
    assert row["adopted_at"] is None and row["rejected_at"] is not None and row["rejection_reason"] == "定位不符"



def test_activation_sample_background_does_not_block_tenant_switch(
    activation_client, conn, monkeypatch,
):
    cfg = main._g_ctx["cfg"]
    cfg.llm.enabled = True
    monkeypatch.setenv(cfg.llm.api_key_env, "test-only-key")
    from src.core import content_pipeline
    from src.core.tenant_switch_gate import TenantSwitchGate

    gate = TenantSwitchGate()
    monkeypatch.setattr(main, "_tenant_switch_gate", gate)
    started = threading.Event()
    release = threading.Event()
    calls = {"count": 0}
    base_body = (
        "个人成长 实用经验 产品经理 AI工具 需求分析 用户研究 项目管理 工作效率 数据分析 "
        "业务流程 产品设计 用户反馈 方案验证 目标拆解 方法总结 实践案例 场景分析 效率提升 团队协作 持续迭代 经验复盘"
    )
    variations = [
        "第一篇重点讲如何把复杂需求拆成可验证的小任务，并通过访谈和数据确认优先级。",
        "第二篇重点讲如何建立团队协作节奏，明确责任边界，并减少重复沟通和返工。",
        "第三篇重点讲如何复盘项目结果，把有效经验沉淀成模板并持续优化执行流程。",
    ]

    def slow_varied(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            started.set()
            assert release.wait(5), "测试未及时释放模拟模型调用"
        return (
            f"快速体验样稿标题 {calls['count']}",
            base_body + " " + variations[(calls["count"] - 1) % len(variations)],
            ["产品经理", "实用经验"],
        )

    monkeypatch.setattr(content_pipeline, "generate_copy_content", slow_varied)
    response = activation_client.post("/api/activation/samples", json={
        "account_description": "分享产品经理的 AI 工具实践和效率方法",
        "platform": "xhs",
        "idempotency_key": "switch-during-sample-generation",
    })
    assert response.status_code == 202
    run_id = response.json()["sample_run_id"]

    try:
        assert started.wait(2), "后台样稿生成没有启动"
        # HTTP 请求已经返回；后台生成仍在运行，但不应继续占用前台切换闸门。
        assert gate.active_requests == 0
        asyncio.run(gate.begin_switch(timeout_seconds=0.2))
        gate.end_switch()
    finally:
        release.set()
        if gate.switching:
            gate.end_switch()

    result = None
    for _ in range(150):
        result = activation_client.get(f"/api/activation/samples/{run_id}").json()
        if result["run"]["status"] in ("ready", "failed"):
            break
        time.sleep(0.02)
    assert result is not None
    assert result["run"]["status"] == "ready", result["run"]
    assert conn.execute(
        "SELECT COUNT(*) FROM sample_contents WHERE sample_run_id=?", (run_id,)
    ).fetchone()[0] == 3
