from __future__ import annotations


def test_content_plan_create_list_update_and_cancel_are_tenant_scoped(client):
    tc, conn, _ = client
    before_drafts = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
    payload = {
        "title": "本周产品内容计划",
        "topic": "围绕真实使用场景制作系列内容",
        "platforms": ["xhs", "douyin"],
        "planned_count": 3,
        "scheduled_date": "2026-11-02",
        "notes": "先做用户问题梳理",
    }
    created = tc.post("/api/content-plans", json=payload)
    assert created.status_code == 200
    plan = created.json()
    assert plan["status"] == "planned"
    assert plan["version"] == 1
    assert plan["platforms"] == ["douyin", "xhs"]

    listed = tc.get("/api/content-plans?start_date=2026-11-01&end_date=2026-11-03")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == plan["id"]

    updated = tc.patch(
        f"/api/content-plans/{plan['id']}",
        json={"version": 1, "scheduled_date": "2026-11-03", "status": "generating"},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["status"] == "generating"

    stale = tc.patch(
        f"/api/content-plans/{plan['id']}",
        json={"version": 1, "title": "过期窗口覆盖"},
    )
    assert stale.status_code == 409

    cancelled = tc.delete(f"/api/content-plans/{plan['id']}?version=2")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == before_drafts


def test_content_plan_rejects_invalid_payload_and_platform(client):
    tc, _, _ = client
    invalid_date = tc.post("/api/content-plans", json={
        "title": "计划", "scheduled_date": "2026-13-40",
    })
    assert invalid_date.status_code == 422

    invalid_count = tc.post("/api/content-plans", json={
        "title": "计划", "scheduled_date": "2026-11-02", "planned_count": 101,
    })
    assert invalid_count.status_code == 422

    invalid_platform = tc.post("/api/content-plans", json={
        "title": "计划", "scheduled_date": "2026-11-02", "platforms": ["not-a-platform"],
    })
    assert invalid_platform.status_code == 422



def test_content_plan_links_generated_drafts_idempotently_and_rolls_back_invalid_batch(client):
    tc, conn, seed = client
    created = tc.post("/api/content-plans", json={
        "title": "多平台内容计划", "scheduled_date": "2026-11-04",
        "platforms": ["xhs"], "planned_count": 2,
    })
    assert created.status_code == 200
    plan_id = created.json()["id"]
    round_id = seed.round()
    draft_id = seed.draft(round_id, platform="xhs", draft_status="draft_saved")

    missing = tc.post(f"/api/content-plans/{plan_id}/drafts", json={
        "draft_ids": [draft_id, "missing-draft"],
    })
    assert missing.status_code == 404
    assert conn.execute(
        "SELECT COUNT(*) FROM content_plan_drafts WHERE plan_id=?", (plan_id,)
    ).fetchone()[0] == 0

    linked = tc.post(f"/api/content-plans/{plan_id}/drafts", json={"draft_ids": [draft_id]})
    assert linked.status_code == 200
    assert linked.json()["linked_count"] == 1
    duplicate = tc.post(f"/api/content-plans/{plan_id}/drafts", json={"draft_ids": [draft_id]})
    assert duplicate.status_code == 200
    assert duplicate.json()["linked_count"] == 0

    detail = tc.get(f"/api/content-plans/{plan_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "generated"
    assert detail.json()["drafts"][0]["draft_id"] == draft_id
    assert detail.json()["drafts"][0]["round_id"] == round_id
