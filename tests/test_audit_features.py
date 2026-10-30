"""深度审查后补齐的功能：选题池管理、内容/轮次删除、Agent 编排、母计划驱动生产、8 点诊断、目标拆分。"""

from __future__ import annotations

import asyncio
import csv
import json

import pytest
from fastapi.testclient import TestClient

from src.adapters.base import DraftResult, Platform
from src.api import main as api_main
from src.core import orchestration as orch
from src.core.content_admin import DeleteBlocked, delete_contents, delete_round
from src.core.guard.ultimate_goal_checker import UltimateGoalChecker
from src.core.notify import Notifier
from src.core.plan_version import PlanVersionManager, suggest_plan_from_goal
from src.core.round_executor import RoundExecutor
from src.core.scoring.topic_eval import ScoringEngine
from src.core.state_machine import DraftStateMachine, RoundStateMachine
from src.core.topic_pool import add_manual_topic, exclude_topics, list_topics, load_pool, purge_topics, restore_topics


@pytest.fixture()
def kb(cfg, tmp_path):
    """临时 CSV 选题库：A 维度 4 条、B 维度 3 条。"""
    path = tmp_path / "kb.csv"
    persona = (cfg.ultimate_goal.persona_keywords or ["AI"])[0]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source", "title", "summary", "link", "date", "dimension"])
        w.writeheader()
        for i in range(4):
            w.writerow({"source": "kb", "title": f"{persona} 方向选题 A{i}", "summary": "s",
                        "link": f"https://e.com/a{i}", "date": "2026-10-20", "dimension": "维度A"})
        for i in range(3):
            w.writerow({"source": "kb", "title": f"{persona} 方向选题 B{i}", "summary": "s",
                        "link": f"https://e.com/b{i}", "date": "2026-10-20", "dimension": "维度B"})
    cfg.topic_kb.path = str(path)
    cfg.topic_kb.type = "csv"
    cfg.topic_kb.field_mapping = {}
    return path


# ---------------- 选题池 ----------------

def test_pool_merges_sources_and_soft_delete(cfg, conn, kb):
    add_manual_topic(conn, title="手动录入的一个选题", dimension="维度B")
    conn.execute("INSERT INTO topic_index (id, title, dimension, source, link) "
                 "VALUES ('hot-x', '热点转入的选题', '维度A', 'hot:weibo', 'https://w.com/1')")
    conn.commit()
    items, rep = load_pool(cfg, conn)
    assert rep["kb_count"] == 7 and rep["extra_count"] == 2 and len(items) == 9

    res = exclude_topics(cfg, conn, ["https://e.com/a0", "hot-x"], "不想做")
    assert set(res["excluded"]) == {"https://e.com/a0", "hot-x"}
    ids = {i.id for i in load_pool(cfg, conn)[0]}
    assert "https://e.com/a0" not in ids and "hot-x" not in ids
    lst = list_topics(cfg, conn, status="excluded")
    assert lst["total"] == 2 and lst["items"][0]["excluded_reason"] == "不想做"

    restore_topics(conn, ["https://e.com/a0"])
    assert "https://e.com/a0" in {i.id for i in load_pool(cfg, conn)[0]}

    # 彻底删除只对手动/热点生效，KB 选题保留
    out = purge_topics(conn, ["hot-x", "https://e.com/a0"])
    assert out["deleted"] == ["hot-x"] and out["kept"] == ["https://e.com/a0"]


def test_manual_topic_validation(conn):
    with pytest.raises(ValueError):
        add_manual_topic(conn, title="短", dimension="A")
    with pytest.raises(ValueError):
        add_manual_topic(conn, title="够长的标题", dimension="")


# ---------------- 删除 ----------------

def test_delete_content_rules(cfg, conn, seed):
    rid = seed.round(status="awaiting_publish")
    seed.draft(rid, draft_status="draft_saved", fetch_status=None, topic_id="t1")
    seed.draft(rid, draft_status="published", topic_id="t2", platform="douyin")
    cids = {r[1]: r[0] for r in conn.execute("SELECT id, topic_id FROM contents")}
    res = delete_contents(cfg, conn, [cids["t1"], cids["t2"], "nope"])
    assert res["deleted"] == [cids["t1"]]
    assert res["blocked"][0]["id"] == cids["t2"]
    assert res["platform_drafts_left"] == 1 and res["not_found"] == ["nope"]
    with pytest.raises(DeleteBlocked):
        delete_round(cfg, conn, rid)   # 还有已发布内容


def test_delete_round(cfg, conn, seed):
    rid = seed.round(status="failed")
    seed.draft(rid, draft_status="failed", fetch_status=None)
    out = delete_round(cfg, conn, rid)
    assert out["contents_deleted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM spiral_rounds WHERE id = ?", (rid,)).fetchone()[0] == 0
    busy = seed.round(status="producing")
    with pytest.raises(DeleteBlocked):
        delete_round(cfg, conn, busy)


# ---------------- 编排 ----------------

def test_orchestration_validation(conn):
    steps = orch.default_steps()
    assert orch.validate_steps(steps) == []
    bad = [dict(s, enabled=False) if s["id"] == "image_card" else s for s in steps]
    assert any("图片卡片" in e for e in orch.validate_steps(bad))     # 存草稿依赖出图
    locked = [dict(s, enabled=False) if s["id"] == "quality_gate" else s for s in steps]
    assert any("锁定" in e for e in orch.validate_steps(locked))
    order = [s for s in steps if s["id"] != "save_draft"]
    order.insert(1, {"id": "save_draft", "enabled": True})
    assert any("之后" in e for e in orch.validate_steps(order))
    # 合法改动：先出图再质检 + 关掉存草稿与后续
    ok = [{"id": i, "enabled": i not in ("save_draft", "auto_schedule", "await_publish")}
          for i in ["topic_eval", "copywriting", "image_card", "quality_gate", "save_draft",
                    "auto_schedule", "await_publish"]]
    assert orch.validate_steps(ok) == []
    orch.save_steps(conn, ok)
    o = orch.Orchestration.load(conn)
    assert o.card_before_gate and not o.on("save_draft")


def test_diagnostics_has_11_items(cfg, conn, kb):
    d = orch.diagnose(cfg, conn)
    assert [i["no"] for i in d["items"]] == list(range(1, 12))
    assert d["items"][0]["status"] in ("pass", "warn", "block")
    assert d["overall"] in ("pass", "warn", "block")


# ---------------- 母计划驱动生产 ----------------

class FakeAdapter:
    def __init__(self):
        self.saved = []

    async def save_draft(self, content):
        self.saved.append(content)
        return DraftResult(success=True, platform_draft_id=f"pd-{len(self.saved)}")


def _executor(cfg, conn):
    adapters = {Platform.XHS: FakeAdapter(), Platform.DOUYIN: FakeAdapter()}
    return RoundExecutor(cfg, conn, ScoringEngine(cfg, conn), UltimateGoalChecker(cfg), adapters,
                         RoundStateMachine(conn), DraftStateMachine(conn)), adapters


def test_executor_follows_plan(cfg, conn, seed, kb, monkeypatch):
    monkeypatch.setattr("src.core.login_manager.has_login_state", lambda _platform: True)
    for p in ("xhs", "douyin"):
        getattr(cfg.platforms, p).enabled = True
    cfg.llm.enabled = False
    monkeypatch.setattr("src.core.round_executor._llm_available", lambda _cfg: True)
    monkeypatch.setattr(
        "src.core.round_executor.generate_copy_content",
        lambda *args, **kwargs: (
            "测试标题",
            "个人成长与实用经验：拆解一个真实问题，给出具体步骤、适用条件和可验证结果。" * 8,
            ["个人成长", "实用经验"],
        ),
    )
    monkeypatch.setattr("src.core.round_executor.make_card_set",
                        lambda title, dim, body, out, platform, footer="": [out / f"{abs(hash(title))}.png"])
    pvm = PlanVersionManager(cfg, conn)
    pvm.save_plan(pvm.build_plan(
        topic_count_per_platform=3, platforms=["xhs", "douyin"],
        dimension_allocation={"维度B": 2},
        per_platform={"douyin": {"topic_count": 1, "publish_slots": ["20:00"]}},
    ))
    steps = orch.default_steps()
    for s in steps:
        if s["id"] == "auto_schedule":
            s["enabled"] = True
    orch.save_steps(conn, steps)

    rid = seed.round(status="created")
    ex, adapters = _executor(cfg, conn)
    report = asyncio.run(ex.run(rid))         # 不传参 = 按母计划
    assert report["topic_count_requested"] == 3 and report["plan_source"] == "plan_settings"
    dims = [s["dimension"] for s in next(x for x in report["steps"] if x["step"] == "score")["selected"]]
    assert dims.count("维度B") >= 2                                     # 维度配额生效
    per_platform = dict(conn.execute("SELECT platform, COUNT(*) FROM contents WHERE round_id = ? GROUP BY platform",
                                     (rid,)).fetchall())
    assert per_platform == {"xhs": 3, "douyin": 1}                     # 平台分项篇数生效
    sched = conn.execute("SELECT scheduled_publish_at FROM drafts WHERE platform='douyin'").fetchone()[0]
    assert sched and sched.endswith("20:00:00")                         # 自动排期生效
    row = conn.execute("SELECT plan_signature, run_report FROM spiral_rounds WHERE id = ?", (rid,)).fetchone()
    assert row[0] == pvm.load_plan()[1]                                  # 轮次签名与母计划一致，不误判失效
    assert json.loads(row[1])["step_status"]["save_draft"]["status"] == "success"


def test_excluded_topic_not_produced(cfg, conn, seed, kb, monkeypatch):
    cfg.platforms.xhs.enabled = True
    cfg.llm.enabled = False
    monkeypatch.setattr("src.core.round_executor.make_card_set",
                        lambda title, dim, body, out, platform, footer="": [out / "x.png"])
    all_ids = [i.id for i in load_pool(cfg, conn)[0]]
    exclude_topics(cfg, conn, all_ids[:-1])
    steps = [dict(s, enabled=s["id"] not in ("save_draft", "auto_schedule", "await_publish"))
             for s in orch.default_steps()]
    orch.save_steps(conn, steps)
    rid = seed.round(status="created")
    ex, adapters = _executor(cfg, conn)
    report = asyncio.run(ex.run(rid, topic_count=5, platform_values=["xhs"]))
    used = {r[0] for r in conn.execute("SELECT topic_id FROM contents WHERE round_id = ?", (rid,))}
    assert used == {all_ids[-1]}
    assert not adapters[Platform.XHS].saved                             # 编排关掉存草稿 = 干跑
    assert report["step_status"]["save_draft"]["status"] == "skipped"


def test_goal_split_suggestion(cfg, conn):
    conn.execute(
        "INSERT INTO goals (id, goal_text, target_metric, target_value, metric_scope, baseline_value, "
        "current_value, metric_period_end, status) VALUES ('g1', '涨粉500', 'new_followers', 500, 'delta', 0, 0, "
        "date('now', '+28 days'), 'active')"
    )
    conn.commit()
    out = suggest_plan_from_goal(cfg, conn, "g1", {"topic_count_per_platform": 5, "enabled_platforms": ["xhs"]})
    cad = out["suggestion"]["cadence"]
    assert cad["goal_id"] == "g1" and cad["rounds_planned"] >= 1 and cad["round_interval_days"] >= 1
    assert out["notes"]


# ---------------- HTTP ----------------

@pytest.fixture()
def client(cfg, conn, kb, test_auth):
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "cfg": cfg, "conn": conn, "sm_draft": DraftStateMachine(conn),
        "plan_version_manager": PlanVersionManager(cfg, conn), "notifier": Notifier(cfg, conn),
        "cb": None, "guard": UltimateGoalChecker(cfg), "scoring": ScoringEngine(cfg, conn),
    })
    try:
        yield test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1"))
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def test_http_topics_crud(client):
    r = client.post("/api/topics", json={"title": "一个手动录入选题", "dimension": "维度A"})
    assert r.status_code == 200 and r.json()["created"]
    tid = r.json()["id"]
    assert client.get("/api/topics").json()["total"] == 8
    assert client.delete(f"/api/topics/{tid}").status_code == 200
    assert client.get("/api/topics", params={"status": "excluded"}).json()["total"] == 1
    assert client.post("/api/topics/restore", json={"ids": [tid]}).status_code == 200
    assert client.get("/api/topics/score", params={"limit": 3}).json()["topics"].__len__() == 3


def test_http_orchestration_and_diagnostics(client):
    v = client.get("/api/orchestration").json()
    assert len(v["steps"]) == 7
    bad = [dict(id=s["id"], enabled=s["id"] != "image_card") for s in v["steps"]]
    assert client.put("/api/orchestration", json={"steps": bad}).status_code == 400
    assert client.post("/api/orchestration/validate", json={"steps": bad}).json()["valid"] is False
    assert client.post("/api/diagnostics", json={}).json()["items"].__len__() == 11
    assert client.get("/api/orchestration/runs").status_code == 200


def test_http_plan_v2(client):
    r = client.post("/api/plans", json={
        "topic_count_per_platform": 4, "enabled_platforms": ["xhs"],
        "dimension_allocation": {"维度A": 2},
        "per_platform": {"xhs": {"topic_count": 3, "publish_slots": ["19:30"]}},
        "cadence": {"round_interval_days": 3, "rounds_planned": 6},
    })
    assert r.status_code == 200, r.text
    p = client.get("/api/plans").json()
    assert p["plan"]["cadence"]["rounds_planned"] == 6
    assert p["per_platform"]["xhs"]["topic_count"] == 3 and p["per_platform"]["xhs"]["publish_slots"] == ["19:30"]
    bad = client.post("/api/plans", json={"topic_count_per_platform": 2, "dimension_allocation": {"维度A": 3}})
    assert bad.status_code == 400
    bad_slot = client.post("/api/plans", json={"topic_count_per_platform": 2,
                                               "per_platform": {"xhs": {"publish_slots": ["25:00"]}}})
    assert bad_slot.status_code == 422
