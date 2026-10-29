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
from src.core.state_machine import DraftStateMachine, RoundStateMachine


@pytest.fixture()
def client(cfg, conn, seed, test_auth):
    """注入临时依赖的 TestClient（不执行 lifespan），并建立受保护会话。"""
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "cfg": cfg,
        "conn": conn,
        "sm_draft": DraftStateMachine(conn),
        "sm_round": RoundStateMachine(conn),
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


def test_failed_metrics_fetch_does_not_make_published_history_deletable(client):
    tc, conn, seed = client
    round_id = seed.round(status="partial_completed")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="failed",
        fetch_status=None, topic_id="published-history",
    )
    conn.execute(
        "UPDATE drafts SET user_confirmed_at = ?, error_msg = ? WHERE id = ?",
        ("2026-01-01T00:00:00", "数据采集重试耗尽", draft_id),
    )
    conn.commit()
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]

    delete_content_response = tc.delete(f"/api/contents/{content_id}")
    assert delete_content_response.status_code == 409
    assert conn.execute(
        "SELECT 1 FROM contents WHERE id = ?", (content_id,)
    ).fetchone() is not None

    delete_round_response = tc.delete(f"/api/rounds/{round_id}")
    assert delete_round_response.status_code == 409
    assert conn.execute(
        "SELECT 1 FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone() is not None


def test_round_business_states_cannot_be_set_manually(client):
    tc, conn, seed = client
    created_round = seed.round(status="created")
    response = tc.post(
        f"/api/rounds/{created_round}/transition",
        json={"target_status": "producing", "reason": "回归测试"},
    )
    assert response.status_code == 409
    assert "业务流程自动推进" in response.json()["detail"]
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (created_round,)
    ).fetchone()[0] == "created"

    producing_round = seed.round(status="producing")
    seed.draft(
        producing_round, platform="xhs", draft_status="draft_pending", fetch_status=None,
    )
    response = tc.post(
        f"/api/rounds/{producing_round}/transition",
        json={"target_status": "awaiting_publish", "reason": "跳过存草稿"},
    )
    assert response.status_code == 409
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (producing_round,)
    ).fetchone()[0] == "producing"


def test_round_cancel_rejected_while_run_is_active(client):
    tc, conn, seed = client
    round_id = seed.round(status="producing")
    api_main._running_rounds.add(round_id)
    try:
        response = tc.post(
            f"/api/rounds/{round_id}/transition",
            json={"target_status": "cancelled", "reason": "回归测试"},
        )
    finally:
        api_main._running_rounds.discard(round_id)

    assert response.status_code == 409
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "producing"


def test_round_cancel_rejected_while_platform_draft_is_saved(client):
    tc, conn, seed = client
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_saved", fetch_status=None,
    )
    response = tc.post(
        f"/api/rounds/{round_id}/transition",
        json={"target_status": "cancelled", "reason": "回归测试"},
    )

    assert response.status_code == 409
    assert "已存入平台" in response.json()["detail"]
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "awaiting_publish"
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_saved"


def test_round_cancel_rejected_if_published_history_exists_even_when_failed(client):
    tc, conn, seed = client
    round_id = seed.round(status="collecting_metrics")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="failed", fetch_status=None,
    )
    conn.execute(
        "UPDATE drafts SET user_confirmed_at = ?, error_msg = ? WHERE id = ?",
        ("2026-01-01T00:00:00", "采集重试耗尽", draft_id),
    )
    conn.commit()

    response = tc.post(
        f"/api/rounds/{round_id}/transition",
        json={"target_status": "cancelled", "reason": "回归测试"},
    )

    assert response.status_code == 409
    assert "存在发布历史" in response.json()["detail"]
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "collecting_metrics"


def test_cancel_round_cancels_local_pending_drafts_together(client):
    tc, conn, seed = client
    round_id = seed.round(status="producing")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_pending", fetch_status=None,
    )
    response = tc.post(
        f"/api/rounds/{round_id}/transition",
        json={"target_status": "cancelled", "reason": "用户取消本轮"},
    )

    assert response.status_code == 200
    assert response.json()["new_status"] == "cancelled"
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "cancelled"
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "cancelled"


def test_round_cancel_rejected_while_run_is_active(client):
    tc, conn, seed = client
    round_id = seed.round(status="producing")
    api_main._running_rounds.add(round_id)
    try:
        response = tc.post(
            f"/api/rounds/{round_id}/transition",
            json={"target_status": "cancelled", "reason": "回归测试"},
        )
    finally:
        api_main._running_rounds.discard(round_id)

    assert response.status_code == 409
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "producing"


def test_round_cancel_rejected_while_platform_draft_is_saved(client):
    tc, conn, seed = client
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_saved", fetch_status=None,
    )
    response = tc.post(
        f"/api/rounds/{round_id}/transition",
        json={"target_status": "cancelled", "reason": "回归测试"},
    )

    assert response.status_code == 409
    assert "已存入平台" in response.json()["detail"]
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "awaiting_publish"
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_saved"


def test_round_cancel_rejected_if_published_history_exists_even_when_failed(client):
    tc, conn, seed = client
    round_id = seed.round(status="collecting_metrics")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="failed", fetch_status=None,
    )
    conn.execute(
        "UPDATE drafts SET user_confirmed_at = ?, error_msg = ? WHERE id = ?",
        ("2026-01-01T00:00:00", "采集重试耗尽", draft_id),
    )
    conn.commit()

    response = tc.post(
        f"/api/rounds/{round_id}/transition",
        json={"target_status": "cancelled", "reason": "回归测试"},
    )

    assert response.status_code == 409
    assert "存在发布历史" in response.json()["detail"]
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "collecting_metrics"


def test_cancel_round_cancels_local_pending_drafts_together(client):
    tc, conn, seed = client
    round_id = seed.round(status="producing")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_pending", fetch_status=None,
    )
    response = tc.post(
        f"/api/rounds/{round_id}/transition",
        json={"target_status": "cancelled", "reason": "用户取消本轮"},
    )

    assert response.status_code == 200
    assert response.json()["new_status"] == "cancelled"
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "cancelled"
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "cancelled"


def test_publish_confirmation_requires_current_quality_gate_or_audited_override(client):
    tc, conn, seed = client
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_saved", fetch_status=None,
    )
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE drafts SET saved_content_hash = ? WHERE id = ?",
        (_current_platform_payload_hash(conn, content_id), draft_id),
    )
    conn.commit()
    payload = {"note_url": "https://www.xiaohongshu.com/explore/123"}

    # 当前文案未检查：普通确认被拦截，不改变发布/轮次状态。
    response = tc.post(f"/api/drafts/{draft_id}/publish_confirm", json=payload)
    assert response.status_code == 409
    assert "未检查" in response.json()["detail"]
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_saved"

    # 当前质检失败：没有明确例外原因不能静默放行。
    conn.execute(
        "UPDATE contents SET guard_outcome = 'fail', guard_reason = '红线命中' WHERE id = ?",
        (content_id,),
    )
    conn.commit()
    response = tc.post(f"/api/drafts/{draft_id}/publish_confirm", json=payload)
    assert response.status_code == 409
    assert "红线命中" in response.json()["detail"]
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_saved"

    # 如果发布确实已在外部平台发生，用户可提供显式原因登记；状态闭环不丢失，且写入审计。
    override_payload = {
        **payload,
        "quality_override_reason": "实际已发布，保留真实运营数据",
    }
    response = tc.post(f"/api/drafts/{draft_id}/publish_confirm", json=override_payload)
    assert response.status_code == 200
    assert response.json()["status"] == "user_published"
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "user_published"
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "collecting_metrics"
    audit = conn.execute(
        "SELECT override_field, reason, overridden_by FROM user_override "
        "WHERE round_id = ? ORDER BY id DESC LIMIT 1", (round_id,)
    ).fetchone()
    assert audit is not None
    assert audit["override_field"] == "publish_quality_override"
    assert audit["reason"] == "实际已发布，保留真实运营数据"
    assert audit["overridden_by"] == "publish-confirm-ui"

    # 另一个草稿的当前质量门通过后，可走普通确认，无需例外理由。
    other_round = seed.round(status="awaiting_publish")
    other_id = seed.draft(
        other_round, platform="xhs", draft_status="draft_saved",
        fetch_status=None, topic_id="pass-quality",
    )
    other_content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (other_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE drafts SET saved_content_hash = ? WHERE id = ?",
        (_current_platform_payload_hash(conn, other_content_id), other_id),
    )
    conn.execute(
        "UPDATE contents SET guard_outcome = 'pass', guard_reason = '检查通过' WHERE id = ?",
        (other_content_id,),
    )
    conn.commit()
    response = tc.post(f"/api/drafts/{other_id}/publish_confirm", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "user_published"
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (other_round,)
    ).fetchone()[0] == "collecting_metrics"


def test_dry_run_content_can_be_manually_published_and_confirmation_is_idempotent(client):
    tc, conn, seed = client
    round_id = seed.round(status="producing")
    draft_id = seed.draft(
        round_id,
        platform="xhs",
        draft_status="draft_pending",
        fetch_status=None,
        topic_id="manual-publish-from-dry-run",
    )
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE contents SET guard_outcome = 'pass', guard_reason = '当前文案检查通过' WHERE id = ?",
        (content_id,),
    )
    conn.commit()
    payload = {"note_url": "https://www.xiaohongshu.com/explore/manual-post"}

    # 干跑内容没有平台草稿，但用户手动发布后可登记，进入正常采集流程。
    response = tc.post(f"/api/drafts/{draft_id}/publish_confirm", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "user_published"
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "user_published"
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "collecting_metrics"

    transition_log_before = conn.execute(
        "SELECT transition_log FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0]
    confirmed_at_before = conn.execute(
        "SELECT user_confirmed_at FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]

    # 重复提交时返回当前状态，不再覆盖确认时间、不重复推进轮次或重新触发发布副作用。
    response = tc.post(f"/api/drafts/{draft_id}/publish_confirm", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "user_published"
    assert response.json()["already_confirmed"] is True
    assert conn.execute(
        "SELECT transition_log FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == transition_log_before
    assert conn.execute(
        "SELECT user_confirmed_at FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == confirmed_at_before


def test_manual_rejection_blocks_normal_publish_until_resubmitted_or_overridden(client):
    tc, conn, seed = client
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_saved", fetch_status=None,
    )
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE drafts SET saved_content_hash = ? WHERE id = ?",
        (_current_platform_payload_hash(conn, content_id), draft_id),
    )
    conn.execute(
        "UPDATE contents SET guard_outcome = 'pass', guard_reason = 'G2 通过' WHERE id = ?",
        (content_id,),
    )
    conn.commit()

    rejected = tc.post(
        f"/api/contents/{content_id}/reject",
        json={"reason": "观点不符合账号定位"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["review_rejected"] is True
    content = conn.execute(
        "SELECT review_rejected, review_rejection_reason FROM contents WHERE id = ?",
        (content_id,),
    ).fetchone()
    assert content["review_rejected"] == 1
    assert content["review_rejection_reason"] == "观点不符合账号定位"
    assert conn.execute(
        "SELECT error_msg FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0].startswith("[已驳回]")

    listed = tc.get("/api/drafts").json()
    draft = next(item for item in listed if item["id"] == draft_id)
    assert draft["review_rejected"] is True
    assert draft["guard_confirmation_allowed"] is False

    normal_confirm = tc.post(
        f"/api/drafts/{draft_id}/publish_confirm",
        json={"note_url": "https://www.xiaohongshu.com/explore/rejected"},
    )
    assert normal_confirm.status_code == 409
    assert "已被人工驳回" in normal_confirm.json()["detail"]
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_saved"

    # 只有明确说明“外部平台已真实发布”的例外原因，才能登记真实发布与采集。
    override = tc.post(
        f"/api/drafts/{draft_id}/publish_confirm",
        json={
            "note_url": "https://www.xiaohongshu.com/explore/rejected",
            "quality_override_reason": "平台实际已发布，保留真实运营记录",
        },
    )
    assert override.status_code == 200
    assert override.json()["status"] == "user_published"
    audit = conn.execute(
        "SELECT override_field, reason FROM user_override "
        "WHERE round_id = ? ORDER BY id DESC LIMIT 1", (round_id,)
    ).fetchone()
    assert audit["override_field"] == "publish_review_rejection_override"
    assert audit["reason"] == "平台实际已发布，保留真实运营记录"


def test_rejected_content_must_pass_g2_before_resubmission(client):
    tc, conn, seed = client
    round_id = seed.round(status="producing")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_pending", fetch_status=None,
    )
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    rejected = tc.post(
        f"/api/contents/{content_id}/reject",
        json={"reason": "需要重新论证"},
    )
    assert rejected.status_code == 200

    blocked = tc.post(
        f"/api/contents/{content_id}/resubmit",
        json={"reason": "我已修改"},
    )
    assert blocked.status_code == 409
    assert "须先修改文案并重新检查通过" in blocked.json()["detail"]
    assert conn.execute(
        "SELECT review_rejected FROM contents WHERE id = ?", (content_id,)
    ).fetchone()[0] == 1

    conn.execute(
        "UPDATE contents SET guard_outcome = 'pass', guard_reason = '重新检查通过' WHERE id = ?",
        (content_id,),
    )
    conn.commit()
    resubmitted = tc.post(
        f"/api/contents/{content_id}/resubmit",
        json={"reason": "已修改并通过目标检查"},
    )
    assert resubmitted.status_code == 200
    row = conn.execute(
        "SELECT review_rejected, review_rejection_reason FROM contents WHERE id = ?",
        (content_id,),
    ).fetchone()
    assert row["review_rejected"] == 0
    assert row["review_rejection_reason"] is None
    assert conn.execute(
        "SELECT error_msg FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_pending"
    audit = conn.execute(
        "SELECT override_field, reason FROM user_override "
        "WHERE round_id = ? ORDER BY id DESC LIMIT 1", (round_id,)
    ).fetchone()
    assert audit["override_field"] == "review_resubmit"
    assert audit["reason"] == "已修改并通过目标检查"


def test_content_mutations_are_rejected_while_round_is_running(client):
    tc, conn, seed = client
    round_id = seed.round(status="producing")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_pending", fetch_status=None,
    )
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    api_main._running_rounds.add(round_id)
    try:
        edit = tc.patch(
            f"/api/contents/{content_id}",
            json={"title": "并发修改", "reason": "回归测试"},
        )
        reject = tc.post(
            f"/api/contents/{content_id}/reject",
            json={"reason": "并发驳回"},
        )
        delete = tc.delete(f"/api/contents/{content_id}")
    finally:
        api_main._running_rounds.discard(round_id)

    assert edit.status_code == 409
    assert reject.status_code == 409
    assert delete.status_code == 409
    row = conn.execute(
        "SELECT title, review_rejected FROM contents WHERE id = ?", (content_id,)
    ).fetchone()
    assert row["title"] == "T"
    assert row["review_rejected"] == 0
    assert conn.execute(
        "SELECT 1 FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone() is not None


def test_published_content_cannot_be_manually_rejected(client):
    tc, conn, seed = client
    round_id = seed.round(status="collecting_metrics")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="user_published", fetch_status=None,
    )
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    response = tc.post(
        f"/api/contents/{content_id}/reject",
        json={"reason": "不应覆盖已发布历史"},
    )
    assert response.status_code == 409
    assert conn.execute(
        "SELECT review_rejected FROM contents WHERE id = ?", (content_id,)
    ).fetchone()[0] == 0


def _current_platform_payload_hash(conn, content_id: str) -> str:
    from src.core.content_version import content_version_hash, parse_json_list

    row = conn.execute(
        "SELECT title, body, tags, image_paths FROM contents WHERE id = ?",
        (content_id,),
    ).fetchone()
    images = parse_json_list(row["image_paths"])
    payload_images = [str(images[0])] if images and images[0] else []
    return content_version_hash(
        row["title"], row["body"], parse_json_list(row["tags"]), payload_images,
    )


def test_saved_platform_draft_version_mismatch_requires_explicit_override(client):
    tc, conn, seed = client
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_saved", fetch_status=None,
    )
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    old_hash = _current_platform_payload_hash(conn, content_id)
    conn.execute(
        "UPDATE drafts SET saved_content_hash = ? WHERE id = ?", (old_hash, draft_id)
    )
    conn.execute(
        "UPDATE contents SET title = ?, guard_outcome = 'pass', guard_reason = ? WHERE id = ?",
        ("本地改过的新标题", "G2 复检通过", content_id),
    )
    conn.commit()

    listed = tc.get("/api/drafts").json()
    item = next(d for d in listed if d["id"] == draft_id)
    assert item["platform_content_sync"] == "stale"
    assert item["guard_confirmation_allowed"] is True

    response = tc.post(
        f"/api/drafts/{draft_id}/publish_confirm",
        json={"note_url": "https://www.xiaohongshu.com/explore/old-version"},
    )
    assert response.status_code == 409
    assert "本地文案已不同于上次存入平台的版本" in response.json()["detail"]
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_saved"

    # 若外部发布事实上已发生，保留真实采集，但要求单独说明平台版本差异并审计。
    response = tc.post(
        f"/api/drafts/{draft_id}/publish_confirm",
        json={
            "note_url": "https://www.xiaohongshu.com/explore/old-version",
            "platform_sync_override_reason": "实际发布的是平台旧版本，保留真实运营数据",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "user_published"
    audit = conn.execute(
        "SELECT override_field, reason FROM user_override "
        "WHERE round_id = ? ORDER BY id DESC LIMIT 1", (round_id,)
    ).fetchone()
    assert audit["override_field"] == "platform_content_sync_override"
    assert audit["reason"] == "实际发布的是平台旧版本，保留真实运营数据"


def test_legacy_platform_draft_requires_sync_attestation_before_normal_confirm(client):
    tc, conn, seed = client
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_saved", fetch_status=None,
    )
    content_id = conn.execute(
        "SELECT content_id FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE drafts SET saved_content_hash = NULL WHERE id = ?", (draft_id,)
    )
    conn.execute(
        "UPDATE contents SET guard_outcome = 'pass', guard_reason = 'G2 通过' WHERE id = ?",
        (content_id,),
    )
    conn.commit()

    listed = tc.get("/api/drafts").json()
    item = next(d for d in listed if d["id"] == draft_id)
    assert item["platform_content_sync"] == "unknown"
    blocked = tc.post(f"/api/drafts/{draft_id}/publish_confirm", json={})
    assert blocked.status_code == 409
    assert "没有保存版本指纹" in blocked.json()["detail"]

    synced = tc.post(
        f"/api/drafts/{draft_id}/confirm_platform_sync",
        json={"reason": "已人工核对平台草稿并更新为当前版本"},
    )
    assert synced.status_code == 200
    assert synced.json()["platform_content_sync"] == "matched"
    expected_hash = _current_platform_payload_hash(conn, content_id)
    saved_hash = conn.execute(
        "SELECT saved_content_hash FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    assert saved_hash == expected_hash
    saved_title = conn.execute(
        "SELECT saved_platform_title FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    current_title = conn.execute(
        "SELECT title FROM contents WHERE id = ?", (content_id,)
    ).fetchone()[0]
    assert saved_title == current_title

    confirmed = tc.post(f"/api/drafts/{draft_id}/publish_confirm", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "user_published"
    audit = conn.execute(
        "SELECT override_field, reason FROM user_override "
        "WHERE round_id = ? AND override_field = 'platform_content_sync_confirmed' "
        "ORDER BY id DESC LIMIT 1", (round_id,)
    ).fetchone()
    assert audit is not None
    assert audit["reason"] == "已人工核对平台草稿并更新为当前版本"


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
