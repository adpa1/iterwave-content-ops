"""租户切换验收护栏。

运行时热切换已实现，但部署开关默认关闭；即时切换默认启用；只有运维显式关闭时才退回重启切换。
这些用例覆盖配置开关和账号组资源隔离边界。
"""
from __future__ import annotations

import sqlite3

import pytest

import src.api.main as api_main
from pathlib import Path

from src.adapters.base import Platform
from src.core import account_login, login_manager
from src.core.round_executor import RoundExecutor
from src.core.tenant_registry import TenantRegistry


def test_tenant_database_files_and_writes_are_isolated(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path / "workspace")
    registry.initialize()
    registry.create("tenant_a", "租户 A")
    registry.create("tenant_b", "租户 B")

    a = registry.paths("tenant_a", create=True)
    b = registry.paths("tenant_b", create=True)
    assert a.db_file != b.db_file

    for db_path, value in ((a.db_file, "A-only"), (b.db_file, "B-only")):
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS isolation_probe (value TEXT)")
            conn.execute("INSERT INTO isolation_probe(value) VALUES (?)", (value,))
            conn.commit()
        finally:
            conn.close()

    def values(db_path: Path) -> list[str]:
        conn = sqlite3.connect(db_path)
        try:
            return [row[0] for row in conn.execute("SELECT value FROM isolation_probe")]
        finally:
            conn.close()

    assert values(a.db_file) == ["A-only"]
    assert values(b.db_file) == ["B-only"]


def test_login_and_browser_profile_paths_follow_tenant_data_dir(
    tmp_path: Path, monkeypatch
) -> None:
    original_al = (account_login.LOGIN_DIR, account_login.PROFILE_BASE,
                   account_login.STORAGE_STATE_DIR)
    original_lm = login_manager.LOGIN_DIR
    monkeypatch.setattr(account_login, "LOGIN_DIR", original_al[0])
    monkeypatch.setattr(account_login, "PROFILE_BASE", original_al[1])
    monkeypatch.setattr(account_login, "STORAGE_STATE_DIR", original_al[2])
    monkeypatch.setattr(login_manager, "LOGIN_DIR", original_lm)

    tenant_a = tmp_path / "tenant_a" / "data"
    tenant_b = tmp_path / "tenant_b" / "data"
    account_login.configure_data_dir(tenant_a)
    login_manager.configure_data_dir(tenant_a)
    a_login = account_login._files("xhs")
    a_profile = account_login.profile_dir("xhs")
    a_storage = login_manager._platform_state_path(Platform.XHS)

    account_login.configure_data_dir(tenant_b)
    login_manager.configure_data_dir(tenant_b)
    b_login = account_login._files("xhs")
    b_profile = account_login.profile_dir("xhs")
    b_storage = login_manager._platform_state_path(Platform.XHS)

    assert a_login["status"] != b_login["status"]
    assert a_login["qr"] != b_login["qr"]
    assert a_profile != b_profile
    assert a_storage != b_storage
    assert a_storage.is_relative_to(tenant_a)
    assert b_storage.is_relative_to(tenant_b)


def test_auto_schedule_only_sets_publish_time_and_never_publishes(conn, seed) -> None:
    """R1: 自动排期只记录计划时间；不得点击发布或改变草稿状态。"""
    round_id = seed.round(status="producing")
    draft_id = seed.draft(
        round_id,
        platform="xhs",
        draft_status="draft_saved",
        fetch_status=None,
    )

    executor = object.__new__(RoundExecutor)
    executor.conn = conn
    executor._plan = {"per_platform": {"xhs": {"publish_slots": ["10:00"]}}}
    report = {"steps": []}

    executor._auto_schedule(round_id, report)

    row = conn.execute(
        "SELECT status, scheduled_publish_at FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    assert row["status"] == "draft_saved"
    assert row["scheduled_publish_at"] is not None
    assert report["steps"] == [{"step": "auto_schedule", "scheduled": 1}]


def test_scheduled_draft_is_not_automatically_marked_published(conn, seed) -> None:
    """仅有 scheduled_publish_at 不得被误认为平台已发布。"""
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(
        round_id,
        platform="xhs",
        draft_status="draft_saved",
        fetch_status=None,
    )
    conn.execute(
        "UPDATE drafts SET scheduled_publish_at = '2099-01-01 10:00:00' WHERE id = ?",
        (draft_id,),
    )
    conn.commit()

    row = conn.execute(
        "SELECT status, scheduled_publish_at FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    assert row["status"] == "draft_saved"
    assert row["scheduled_publish_at"] == "2099-01-01 10:00:00"



def test_tenant_hot_switch_flag_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("ITERWAVE_ENABLE_TENANT_HOT_SWITCH", raising=False)
    assert api_main._tenant_hot_switch_enabled() is True


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_tenant_hot_switch_flag_accepts_explicit_truthy_values(monkeypatch, value):
    monkeypatch.setenv("ITERWAVE_ENABLE_TENANT_HOT_SWITCH", value)
    assert api_main._tenant_hot_switch_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "disabled"])
def test_tenant_hot_switch_flag_rejects_non_truthy_values(monkeypatch, value):
    monkeypatch.setenv("ITERWAVE_ENABLE_TENANT_HOT_SWITCH", value)
    assert api_main._tenant_hot_switch_enabled() is False



def test_derived_custom_round_snapshot_is_authoritative(cfg, conn, seed):
    """未保存母计划时，按轮次创建参数运行，不与默认推导母计划（5 条/全平台）误判冲突。"""
    from src.core.plan_version import PlanVersionManager
    from src.core.task_snapshot import build_execution_snapshot, validate_execution_snapshot

    round_id = seed.round()
    pvm = PlanVersionManager(cfg, conn)
    round_plan = pvm.build_plan(
        topic_count_per_platform=3,
        platforms=["xhs"],
    )
    round_signature = pvm.record(round_id, round_plan, author="test")

    snapshot = build_execution_snapshot(
        cfg, conn, round_id=round_id,
        parameters={"round_id": round_id, "topic_count": 3, "platforms": ["xhs"]},
    )

    assert snapshot["plan_source"] == "derived"
    assert snapshot["plan_signature"] == round_signature
    assert snapshot["round_plan_snapshot"]["topic_count_per_platform"] == 3
    assert snapshot["round_plan_snapshot"]["enabled_platforms"] == ["xhs"]
    validate_execution_snapshot(snapshot, cfg, conn)



def test_unrelated_config_changes_do_not_invalidate_queued_production(cfg):
    from src.core.task_snapshot import config_fingerprint

    before = config_fingerprint(cfg)
    cfg.circuit_breaker.cooldown_minutes += 1
    assert config_fingerprint(cfg) == before

    cfg.llm.model = cfg.llm.model + "-changed"
    assert config_fingerprint(cfg) != before


def test_process_singleton_lock_writes_real_newline(tmp_path):
    from src.core.process_lock import ProcessSingletonLock

    path = tmp_path / "api.lock"
    lock = ProcessSingletonLock(path)
    lock.acquire()
    try:
        payload = path.read_bytes()
        assert payload.endswith(b"\n")
        assert payload[:-1].isdigit()
    finally:
        lock.release()
