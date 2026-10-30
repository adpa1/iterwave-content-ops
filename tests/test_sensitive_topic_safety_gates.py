from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core import hot_topic_service
from src.core.hot_topics import _sensitive_topic_hits
from src.core.guard.ultimate_goal_checker import CheckResult, UltimateGoalChecker


@pytest.fixture()
def client(cfg, conn, test_auth):
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"cfg": cfg, "conn": conn})
    hot_topic_service.reset_for_tests()
    try:
        yield test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1")), conn
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def test_political_party_language_is_sensitive():
    title = "我们党有其他政党均不具备较强优势"
    assert "时政" in _sensitive_topic_hits(title)


def test_sensitive_language_is_blocked_by_content_quality_gate(cfg):
    outcome = UltimateGoalChecker(cfg).check_content(
        "关于我们党与其他政党的比较",
        "这是一篇普通的观点分析。",
        [],
        platform="xhs",
    )
    assert outcome.result == CheckResult.FAIL
    assert "需人工确认" in outcome.reason


def test_sensitive_hot_topic_import_requires_operator_confirmation(client):
    tc, conn = client
    payload = {
        "title": "我们党有其他政党均不具备较强优势",
        "source": "weibo",
        "url": "https://example.com/political-topic",
        "dimension": "社会观察",
    }
    rejected = tc.post("/api/hot_topics/import", json=payload)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "manual_confirmation_required"

    accepted = tc.post(
        "/api/hot_topics/import",
        json={**payload, "confirmed_by": "验收操作员"},
    )
    assert accepted.status_code == 200
    row = conn.execute(
        "SELECT confirmed_by, categories FROM hot_topic_import_audit WHERE title = ?",
        (payload["title"],),
    ).fetchone()
    assert row is not None
    assert row["confirmed_by"] == "验收操作员"
    assert "时政" in row["categories"]


def test_correct_login_is_not_blocked_by_other_users_ip_failures(monkeypatch):
    import src.core.auth as auth

    monkeypatch.setattr(auth, "verify_password", lambda value: value == "correct-secret")
    monkeypatch.setattr(auth, "_new_session", lambda: "session-token")
    monkeypatch.setitem(auth._failures, "shared-nat", [auth.time.time()] * auth.LOGIN_MAX_FAILURES)

    assert auth.login("correct-secret", "shared-nat") == "session-token"
    assert "shared-nat" not in auth._failures



def test_round_run_endpoint_enqueues_global_task(client, seed, monkeypatch, tmp_path):
    import asyncio

    import src.api.main as api_main
    from src.core.task_queue import GlobalTaskQueue

    _tc, _conn = client
    round_id = seed.round(status="created")
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    monkeypatch.setattr(api_main, "_task_queue", queue)
    api_main._round_job_status.pop(round_id, None)

    run_route = next(
        route for route in api_main.app.routes
        if getattr(route, "path", None) == "/api/rounds/{round_id}/run"
    )
    status_route = next(
        route for route in api_main.app.routes
        if getattr(route, "path", None) == "/api/rounds/{round_id}/run_status"
    )

    started = asyncio.run(run_route.endpoint(round_id, api_main.RunRoundRequest()))
    assert started["started"] is True
    assert started["queued"] is True
    assert started["task_id"]

    tenant_id = api_main._g_ctx.get("runtime_tenant_id", "default")
    queued = queue.find_latest(
        tenant_id=tenant_id, task_type="round_run", aggregate_id=str(round_id),
    )
    assert queued is not None and queued.status == "queued"

    claimed = queue.claim_next("test-worker")
    assert claimed is not None and claimed.task_id == started["task_id"]
    report = {"round_id": round_id, "final_status": "success", "steps": []}
    assert queue.finish(claimed.task_id, "test-worker", result=report)
    status = status_route.endpoint(round_id)
    assert status["running"] is False
    assert status["report"]["final_status"] == "success"
    queue.release_worker("test-worker")
