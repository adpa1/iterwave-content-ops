"""账号组运行时切换的原子提交与失败回滚测试。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.main as api_main


class FakeRegistry:
    def __init__(self):
        self.active = "A"
        self.names = {"A": "账号组 A", "B": "账号组 B"}

    def get(self, tenant_id):
        if tenant_id not in self.names:
            raise api_main.TenantError(f"账号组不存在：{tenant_id}")
        return {"id": tenant_id, "name": self.names[tenant_id]}

    def select_active(self, tenant_id):
        item = self.get(tenant_id)
        self.active = tenant_id
        return {"id": tenant_id, "name": item["name"]}


class FakeScheduler:
    def __init__(self, *, fail_start=False):
        self.fail_start = fail_start
        self.started = False
        self.stopped = False
        self.drained = False

    async def quiesce(self, timeout_seconds=30.0):
        self.drained = True
        self.stopped = True
        return True

    def start(self):
        if self.fail_start:
            raise RuntimeError("simulated scheduler start failure")
        self.started = True

    def shutdown(self):
        self.stopped = True


class FakeConnection:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.closed = False

    def close(self):
        self.closed = True


def make_context(registry, tenant_id, *, fail_start=False):
    cfg = SimpleNamespace(
        paths=SimpleNamespace(data_dir=f"/fake/{tenant_id}/data"),
        resolve_path=lambda path: str(path),
    )
    return {
        "tenant_registry": registry,
        "runtime_tenant_id": tenant_id,
        "cfg": cfg,
        "conn": FakeConnection(tenant_id),
        "scheduler": FakeScheduler(fail_start=fail_start),
        "notifier": object(),
    }


def patch_runtime(monkeypatch, registry, contexts):
    monkeypatch.setattr(api_main, "_g_ctx", contexts["A"])
    monkeypatch.setattr(api_main, "_running_rounds", set())
    monkeypatch.setattr(api_main, "set_active_notifier", lambda notifier: None)
    monkeypatch.setattr(
        api_main, "_build_tenant_runtime_context",
        lambda _registry, tenant_id, run_startup_maintenance=False: contexts[tenant_id](),
    )
    from src.core import account_login, login_manager
    monkeypatch.setattr(account_login, "stop_all_logins", lambda timeout_seconds=10.0: None)
    monkeypatch.setattr(account_login, "configure_data_dir", lambda data_dir: None)
    monkeypatch.setattr(login_manager, "configure_data_dir", lambda data_dir: None)


def test_runtime_switch_supports_a_to_b_to_a(monkeypatch):
    registry = FakeRegistry()
    contexts = {
        "A": lambda: make_context(registry, "A"),
        "B": lambda: make_context(registry, "B"),
    }
    original_a = contexts["A"]()
    monkeypatch.setattr(api_main, "_g_ctx", original_a)
    monkeypatch.setattr(api_main, "_running_rounds", set())
    monkeypatch.setattr(api_main, "set_active_notifier", lambda notifier: None)
    monkeypatch.setattr(api_main, "_build_tenant_runtime_context", lambda _r, tid, run_startup_maintenance=False: contexts[tid]())
    from src.core import account_login, login_manager
    monkeypatch.setattr(account_login, "stop_all_logins", lambda timeout_seconds=10.0: None)
    monkeypatch.setattr(account_login, "configure_data_dir", lambda data_dir: None)
    monkeypatch.setattr(login_manager, "configure_data_dir", lambda data_dir: None)

    async def run():
        result_b = await api_main._switch_tenant_runtime("B")
        assert result_b["switch_status"] == "completed"
        assert api_main._g_ctx["runtime_tenant_id"] == "B"
        assert registry.active == "B"
        conn_b = api_main._g_ctx["conn"]
        assert conn_b.tenant_id == "B"

        result_a = await api_main._switch_tenant_runtime("A")
        assert result_a["switch_status"] == "completed"
        assert api_main._g_ctx["runtime_tenant_id"] == "A"
        assert registry.active == "A"
        assert api_main._g_ctx["conn"].tenant_id == "A"
        assert api_main._g_ctx["conn"] is not conn_b

    try:
        asyncio.run(run())
    finally:
        monkeypatch.setattr(api_main, "_g_ctx", {})
        monkeypatch.setattr(api_main, "_running_rounds", set())


def test_runtime_switch_rolls_back_if_target_scheduler_fails(monkeypatch):
    registry = FakeRegistry()
    old = make_context(registry, "A")
    old_scheduler = old["scheduler"]
    old_conn = old["conn"]
    target = make_context(registry, "B", fail_start=True)
    rollback = make_context(registry, "A")
    built = {"count": 0}

    def build(_registry, tenant_id, run_startup_maintenance=False):
        if tenant_id == "B":
            return target
        built["count"] += 1
        return rollback

    monkeypatch.setattr(api_main, "_g_ctx", old)
    monkeypatch.setattr(api_main, "_running_rounds", set())
    monkeypatch.setattr(api_main, "set_active_notifier", lambda notifier: None)
    monkeypatch.setattr(api_main, "_build_tenant_runtime_context", build)
    from src.core import account_login, login_manager
    monkeypatch.setattr(account_login, "stop_all_logins", lambda timeout_seconds=10.0: None)
    monkeypatch.setattr(account_login, "configure_data_dir", lambda data_dir: None)
    monkeypatch.setattr(login_manager, "configure_data_dir", lambda data_dir: None)

    async def run():
        with pytest.raises(HTTPException) as caught:
            await api_main._switch_tenant_runtime("B")
        assert caught.value.status_code == 409
        assert registry.active == "A"
        assert api_main._g_ctx["runtime_tenant_id"] == "A"
        assert api_main._g_ctx["conn"] is old_conn
        assert target["scheduler"].stopped
        assert target["conn"].closed
        # 常驻后台运行时不因前台切换失败而停止或关闭。
        assert not old_scheduler.drained
        assert not old_scheduler.stopped
        assert not old_conn.closed

    try:
        asyncio.run(run())
    finally:
        monkeypatch.setattr(api_main, "_g_ctx", {})
        monkeypatch.setattr(api_main, "_running_rounds", set())


def test_target_initialization_failure_does_not_stop_old_runtime(monkeypatch):
    registry = FakeRegistry()
    old = make_context(registry, "A")
    monkeypatch.setattr(api_main, "_g_ctx", old)
    monkeypatch.setattr(api_main, "_running_rounds", set())
    monkeypatch.setattr(
        api_main, "_build_tenant_runtime_context",
        lambda _registry, tenant_id, run_startup_maintenance=False: (_ for _ in ()).throw(RuntimeError("pending restore")),
    )

    async def run():
        with pytest.raises(HTTPException) as caught:
            await api_main._switch_tenant_runtime("B")
        assert caught.value.status_code == 409
        assert api_main._g_ctx is old
        assert not old["scheduler"].stopped
        assert not old["conn"].closed

    try:
        asyncio.run(run())
    finally:
        monkeypatch.setattr(api_main, "_g_ctx", {})
        monkeypatch.setattr(api_main, "_running_rounds", set())
