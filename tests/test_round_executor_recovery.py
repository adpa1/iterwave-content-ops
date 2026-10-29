"""生产线恢复路径回归测试：干跑后可继续存草稿，复盘异常不会遗留 running 任务。"""

from __future__ import annotations

import asyncio
import json

from src.adapters.base import DraftResult, Platform
from src.core.guard.ultimate_goal_checker import CheckOutcome, CheckResult
from src.core.orchestration import default_steps, save_steps
from src.core.round_executor import RoundExecutor
from src.core.state_machine import DraftStateMachine, RoundStateMachine


class _SuccessfulDraftAdapter:
    def __init__(self):
        self.payloads = []

    async def save_draft(self, payload):
        self.payloads.append(payload)
        return DraftResult(success=True, platform_draft_id="platform-draft-123")


class _PassingGuard:
    def check_content(self, title, body, tags, *, platform):
        return CheckOutcome(result=CheckResult.PASS, reason="测试通过")


class _RejectingGuard:
    def check_content(self, title, body, tags, *, platform):
        return CheckOutcome(result=CheckResult.FAIL, reason="测试命中红线")


def _executor(cfg, conn, adapters=None, guard=None):
    return RoundExecutor(
        cfg=cfg,
        conn=conn,
        scoring=None,
        guard=guard if guard is not None else _PassingGuard(),
        adapters=adapters or {},
        sm_round=RoundStateMachine(conn),
        sm_draft=DraftStateMachine(conn),
    )


def test_dry_run_pending_drafts_can_be_saved_without_regenerating(cfg, conn, seed):
    round_id = seed.round(status="producing")
    draft_id = seed.draft(round_id, platform="xhs", draft_status="draft_pending", fetch_status=None)
    conn.execute(
        "UPDATE contents SET title = ?, body = ?, tags = ?, image_paths = ? "
        "WHERE id = (SELECT content_id FROM drafts WHERE id = ?)",
        ("恢复测试标题", "恢复测试正文", json.dumps(["AI", "运营"]), json.dumps(["/tmp/card.png"]), draft_id),
    )
    conn.execute(
        "UPDATE spiral_rounds SET run_report = ? WHERE id = ?",
        (json.dumps({
            "round_id": round_id,
            "round_number": 1,
            "contents_created": 1,
            "drafts_created": 1,
            "drafts_saved": 0,
            "drafts_failed": 0,
            "errors": [],
            "steps": [
                {"step": "produce", "contents": 1},
                {"step": "dry_run", "note": "本次未执行存草稿"},
            ],
            "final_status": "producing",
        }), round_id),
    )
    conn.commit()

    adapter = _SuccessfulDraftAdapter()
    executor = _executor(cfg, conn, {Platform.XHS: adapter})
    result = asyncio.run(executor.save_pending_drafts(round_id))

    assert result["drafts_attempted"] == 1
    assert result["drafts_saved"] == 1
    assert result["drafts_failed"] == 0
    assert result["pending_drafts"] == 0
    assert result["final_status"] == "awaiting_publish"
    assert len(adapter.payloads) == 1
    assert adapter.payloads[0].title == "恢复测试标题"
    assert adapter.payloads[0].body == "恢复测试正文"
    assert adapter.payloads[0].tags == ["AI", "运营"]
    assert adapter.payloads[0].image_paths == ["/tmp/card.png"]
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_saved"
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "awaiting_publish"

    stored = json.loads(conn.execute(
        "SELECT run_report FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0])
    assert not any(step.get("step") == "dry_run" for step in stored["steps"])
    assert stored["resume_history"][-1]["drafts_saved"] == 1
    assert stored["step_status"]["save_draft"]["status"] == "success"


def test_resume_rechecks_current_content_before_any_platform_save(cfg, conn, seed):
    round_id = seed.round(status="producing")
    draft_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_pending",
        fetch_status=None, topic_id="edited-invalid-content",
    )
    conn.execute(
        "UPDATE contents SET title = ?, body = ?, tags = ?, guard_outcome = NULL, guard_reason = NULL "
        "WHERE id = (SELECT content_id FROM drafts WHERE id = ?)",
        ("编辑后的违规标题", "当前正文", json.dumps(["测试"]), draft_id),
    )
    conn.commit()

    adapter = _SuccessfulDraftAdapter()
    executor = _executor(cfg, conn, {Platform.XHS: adapter}, guard=_RejectingGuard())
    try:
        asyncio.run(executor.save_pending_drafts(round_id))
    except ValueError as exc:
        assert "未通过当前质量门" in str(exc)
        assert "测试命中红线" in str(exc)
    else:
        raise AssertionError("当前内容未通过质量门，不应执行存草稿")

    # 即使质检拦截，也先持久化当前文案的失败结果，方便 UI 展示与修订。
    content = conn.execute(
        "SELECT guard_outcome, guard_reason FROM contents WHERE id = "
        "(SELECT content_id FROM drafts WHERE id = ?)", (draft_id,),
    ).fetchone()
    assert content["guard_outcome"] == "fail"
    assert "测试命中红线" in content["guard_reason"]
    assert adapter.payloads == []  # 质检前不能触发任何平台副作用
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "draft_pending"
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "producing"


def test_resume_merges_draft_ids_instead_of_losing_prior_successes(cfg, conn, seed):
    round_id = seed.round(status="producing")
    previous_saved_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_saved",
        fetch_status=None, topic_id="already-saved",
    )
    pending_id = seed.draft(
        round_id, platform="xhs", draft_status="draft_pending",
        fetch_status=None, topic_id="still-pending",
    )
    conn.execute(
        "UPDATE spiral_rounds SET draft_ids = ?, run_report = ? WHERE id = ?",
        (
            json.dumps([previous_saved_id]),
            json.dumps({
                "round_id": round_id, "round_number": 1,
                "contents_created": 2, "drafts_created": 2,
                "drafts_saved": 1, "drafts_failed": 0, "errors": [],
                "steps": [{"step": "produce", "contents": 2},
                          {"step": "save_drafts", "saved": 1, "failed": 0}],
                "final_status": "producing",
            }),
            round_id,
        ),
    )
    conn.commit()

    adapter = _SuccessfulDraftAdapter()
    result = asyncio.run(_executor(cfg, conn, {Platform.XHS: adapter}).save_pending_drafts(round_id))
    saved_ids = json.loads(conn.execute(
        "SELECT draft_ids FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0])

    assert result["drafts_saved"] == 1
    assert result["final_status"] == "awaiting_publish"
    assert previous_saved_id in saved_ids
    assert pending_id in saved_ids
    assert len(saved_ids) == len(set(saved_ids))


def test_resume_requires_enabled_save_draft_step(cfg, conn, seed):
    round_id = seed.round(status="producing")
    seed.draft(round_id, platform="xhs", draft_status="draft_pending", fetch_status=None)
    steps = default_steps()
    for step in steps:
        if step["id"] in ("save_draft", "await_publish"):
            step["enabled"] = False
    save_steps(conn, steps)

    executor = _executor(cfg, conn, {Platform.XHS: _SuccessfulDraftAdapter()})
    try:
        asyncio.run(executor.save_pending_drafts(round_id))
    except ValueError as exc:
        assert "已关闭" in str(exc)
    else:
        raise AssertionError("关闭存草稿编排时不应执行平台存草稿")
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "producing"
    assert conn.execute(
        "SELECT status FROM drafts WHERE content_id IN "
        "(SELECT id FROM contents WHERE round_id = ?)", (round_id,)
    ).fetchone()[0] == "draft_pending"


def test_feedback_resolution_exception_marks_round_and_task_failed(cfg, conn, seed, monkeypatch):
    round_id = seed.round(status="created")
    executor = _executor(cfg, conn)
    executor._resolve_platforms = lambda values: [Platform.XHS]
    monkeypatch.setattr(
        "src.core.plan_version.PlanVersionManager.load_plan",
        lambda self: ({"topic_count_per_platform": 1, "enabled_platforms": ["xhs"]}, "sig", None, "test"),
    )
    monkeypatch.setattr(
        executor, "_resolve_feedback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("feedback parsing failed")),
    )

    report = asyncio.run(executor.run(round_id))
    assert report["final_status"] == "failed"
    assert any("feedback parsing failed" in msg for msg in report["errors"])
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "failed"
    task = conn.execute(
        "SELECT status FROM task_runs WHERE task_type = 'round_pipeline' AND biz_id = ? "
        "ORDER BY id DESC LIMIT 1", (str(round_id),)
    ).fetchone()
    assert task is not None and task["status"] == "failed"
