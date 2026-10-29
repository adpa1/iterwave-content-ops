"""R-2/R-3/R-4/R-5/R-6 新接口的 HTTP 级用例。

不跑 lifespan（避免真实 config/data 目录），直接把 tmp 库里的模块塞进 `_g_ctx`，
再用 TestClient 发真实请求，验证状态码与落库结果。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core.notify import Notifier
from src.core.ops.pause import is_ops_paused
from src.core.plan_version import PlanVersionManager
from src.core.state_machine import DraftStateMachine


@pytest.fixture()
def client(cfg, conn, seed, test_auth):
    """注入临时依赖的 TestClient（不执行 lifespan），并建立受保护会话。"""
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "cfg": cfg,
        "conn": conn,
        "sm_draft": DraftStateMachine(conn),
        "plan_version_manager": PlanVersionManager(cfg, conn),
        "notifier": Notifier(cfg, conn),
    })
    app = api_main._build_app()
    try:
        tc = test_auth(TestClient(app, base_url="http://127.0.0.1"))
        yield tc, conn, seed
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


# ---------------------------------------------------------------- R-2 采纳缺口
def test_adopt_gaps_rejects_empty(client):
    tc, _, _ = client
    assert tc.post("/api/plan_adjustments/adopt", json={"dimensions": []}).status_code == 400


def test_adopt_gaps_writes_plan_and_changes_signature(client):
    tc, conn, _ = client
    pvm = PlanVersionManager(api_main._g_ctx["cfg"], conn)
    before = pvm.load_plan()[1]

    r = tc.post("/api/plan_adjustments/adopt", json={"dimensions": ["政策解读", "家庭场景"]})
    assert r.status_code == 200
    body = r.json()
    assert body["adopted_dimensions"] == ["政策解读", "家庭场景"]
    assert body["dimension_allocation"]["政策解读"] == 1
    assert body["signature"] != before

    # 签名已持久化到 signature_history（R18 审计追溯）
    rows = conn.execute("SELECT COUNT(*) FROM signature_history").fetchone()[0]
    assert rows >= 1


# ---------------------------------------------------------------- R-3 热点入库
def test_hot_import_requires_evidence(client):
    tc, _, _ = client
    # R22：缺 url → 400
    r = tc.post("/api/hot_topics/import", json={"title": "无链接热点", "source": "weibo"})
    assert r.status_code == 400
    # 缺 source → 400
    r = tc.post("/api/hot_topics/import", json={"title": "无来源", "url": "https://x.com/a"})
    assert r.status_code == 400


def test_hot_import_is_idempotent_by_source_title(client):
    tc, conn, _ = client
    payload = {"title": "秋季流感高发", "source": "weibo",
               "url": "https://s.weibo.com/1", "dimension": "健康"}
    first = tc.post("/api/hot_topics/import", json=payload)
    assert first.status_code == 200 and first.json()["imported"] is True

    second = tc.post("/api/hot_topics/import", json=payload)
    assert second.status_code == 200 and second.json()["imported"] is False

    row = conn.execute(
        "SELECT id, dimension FROM topic_index WHERE title = ?", ("秋季流感高发",)
    ).fetchone()
    assert row is not None
    assert row["id"].startswith("hot-")
    assert row["dimension"] == "健康"


# ---------------------------------------------------------------- R-6 全局暂停
def test_ops_pause_roundtrip_persists(client):
    tc, conn, _ = client
    assert tc.get("/api/ops/pause").json()["paused"] is False
    assert tc.post("/api/ops/pause", json={"paused": True}).json()["paused"] is True
    assert is_ops_paused(conn) is True
    assert tc.get("/api/ops/pause").json()["paused"] is True
    assert tc.post("/api/ops/pause", json={"paused": False}).json()["paused"] is False
    assert is_ops_paused(conn) is False


# ---------------------------------------------------------------- R-4 通知自检
def test_notify_test_reports_channels(client):
    tc, _, _ = client
    r = tc.post("/api/notify/test")
    assert r.status_code == 200
    results = r.json()["results"]
    assert set(results) == {"macos", "wecom", "feishu"}
    assert results["macos"] in {"ok", "failed", "disabled", "unavailable"}


def test_notify_test_503_when_notifier_missing(client):
    tc, _, _ = client
    api_main._g_ctx["notifier"] = None
    assert tc.post("/api/notify/test").status_code == 503


def test_editing_content_invalidates_previous_guard_result(client):
    tc, conn, seed = client
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(round_id, platform="xhs", draft_status="draft_saved", fetch_status=None)
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE contents SET guard_outcome = 'pass', guard_reason = '原稿通过' WHERE id = ?",
        (content_id,),
    )
    conn.commit()

    unchanged = tc.patch(f"/api/contents/{content_id}", json={"title": "T"})
    assert unchanged.status_code == 200 and unchanged.json()["changed"] is False
    row = conn.execute(
        "SELECT guard_outcome, guard_reason FROM contents WHERE id = ?", (content_id,)
    ).fetchone()
    assert row["guard_outcome"] == "pass"

    edited = tc.patch(
        f"/api/contents/{content_id}",
        json={"body": "编辑后的正文", "reason": "测试修改"},
    )
    assert edited.status_code == 200 and edited.json()["changed"] is True
    row = conn.execute(
        "SELECT guard_outcome, guard_reason FROM contents WHERE id = ?", (content_id,)
    ).fetchone()
    assert row["guard_outcome"] is None
    assert row["guard_reason"] is None


def test_published_content_cannot_be_mutated_in_place(client):
    tc, conn, seed = client
    cases = [
        ("user_published", None),
        # 数据采集失败会把草稿状态改成 failed，但确认发布时间必须继续保护历史文案。
        ("failed", "2026-01-01T00:00:00"),
    ]
    for index, (status, confirmed_at) in enumerate(cases):
        round_id = seed.round(status="collecting_metrics")
        draft_id = seed.draft(
            round_id, platform="xhs", draft_status=status, fetch_status=None,
            topic_id=f"immutability-{index}",
        )
        if confirmed_at:
            conn.execute(
                "UPDATE drafts SET user_confirmed_at = ? WHERE id = ?",
                (confirmed_at, draft_id),
            )
            conn.commit()
        content_id = conn.execute(
            "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()[0]

        response = tc.patch(
            f"/api/contents/{content_id}",
            json={"title": "不应覆盖已发布版本", "reason": "回归测试"},
        )
        assert response.status_code == 409
        row = conn.execute(
            "SELECT title, body FROM contents WHERE id = ?", (content_id,)
        ).fetchone()
        assert row["title"] == "T"
        assert row["body"] == "B"


# ---------------------------------------------------------------- R-5 KPI 窗口
def test_ops_kpi_days_param(client):
    tc, _, seed = client
    rid = seed.round("collecting_metrics")
    seed.draft(rid, platform="xhs")
    ok = tc.get("/api/ops_kpi", params={"days": 7})
    assert ok.status_code == 200 and ok.json()["window_days"] == 7
    # 越界参数由 Query(ge=1, le=365) 拦下
    assert tc.get("/api/ops_kpi", params={"days": 0}).status_code == 422
    assert tc.get("/api/ops_kpi", params={"days": 999}).status_code == 422


# ---------------------------------------------------------------- R-17 pending_verify
def test_resolve_pending_verify_branching(client):
    tc, conn, _ = client
    conn.execute(
        "INSERT INTO task_runs (task_type, status, started_at) VALUES ('comment_polling', 'pending_verify', ?)",
        ("2026-10-28 07:00:00",),
    )
    conn.commit()
    run_id = conn.execute("SELECT MAX(id) FROM task_runs").fetchone()[0]

    assert tc.post(f"/api/task_runs/{run_id}/resolve", json={"action": "bogus"}).status_code == 400
    r = tc.post(f"/api/task_runs/{run_id}/resolve", json={"action": "sent"})
    assert r.status_code == 200
    assert conn.execute("SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] == "success"


def test_draft_cancel(client):
    tc, conn, seed = client
    rid = seed.round("awaiting_publish")
    draft_id = seed.draft(rid, draft_status="draft_saved")

    assert tc.post("/api/drafts/nonexistent/cancel").status_code == 404
    r = tc.post(f"/api/drafts/{draft_id}/cancel")
    assert r.status_code == 200
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,)).fetchone()[0] == "cancelled"


def test_scheduler_run_unknown_job_404(client):
    tc, _, _ = client

    class _StubSched:
        class scheduler:  # noqa: N801 - 模拟 APScheduler 属性名
            @staticmethod
            def get_job(_job_id: str):
                return None

    api_main._g_ctx["scheduler"] = _StubSched()
    assert tc.post("/api/scheduler/jobs/not_a_job/run").status_code == 404
