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



def test_round_run_endpoint_returns_before_background_execution(client, seed, monkeypatch):
    import asyncio

    import src.api.main as api_main

    tc, _conn = client
    round_id = seed.round(status="created")

    class FakeExecutor:
        async def run(self, *_args, **_kwargs):
            await asyncio.sleep(0.02)
            return {"round_id": round_id, "final_status": "success", "steps": []}

    monkeypatch.setitem(api_main._g_ctx, "executor", FakeExecutor())
    api_main._round_job_status.pop(round_id, None)
    response = tc.post(f"/api/rounds/{round_id}/run", json={})
    assert response.status_code == 202
    assert response.json()["running"] is True

    status = None
    for _ in range(20):
        status = tc.get(f"/api/rounds/{round_id}/run_status").json()
        if not status["running"]:
            break
    assert status is not None
    assert status["report"]["final_status"] == "success"
