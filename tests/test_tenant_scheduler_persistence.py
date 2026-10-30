from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import src.api.main as api_main


class _Registry:
    def __init__(self):
        self.active = "tenant-a"

    def get(self, tenant_id):
        return {"id": tenant_id, "name": tenant_id}

    def select_active(self, tenant_id):
        self.active = tenant_id
        return {"id": tenant_id, "name": tenant_id}


class _Scheduler:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def shutdown(self):
        self.stopped += 1


class _Connection:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _Config:
    def __init__(self, root: Path):
        self.paths = SimpleNamespace(data_dir=str(root / "data"))

    def resolve_path(self, value):
        return value


@pytest.mark.asyncio
async def test_frontend_switch_keeps_previous_tenant_scheduler_and_connection(monkeypatch, tmp_path):
    old_context = {
        "tenant_registry": _Registry(),
        "runtime_tenant_id": "tenant-a",
        "cfg": _Config(tmp_path / "tenant-a"),
        "scheduler": _Scheduler(),
        "conn": _Connection(),
        "notifier": object(),
    }
    new_context = {
        "tenant_registry": old_context["tenant_registry"],
        "runtime_tenant_id": "tenant-b",
        "cfg": _Config(tmp_path / "tenant-b"),
        "scheduler": _Scheduler(),
        "conn": _Connection(),
        "notifier": object(),
    }

    previous_context = dict(api_main._g_ctx)
    previous_runtimes = dict(api_main._tenant_runtime_contexts)
    try:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(old_context)
        api_main._tenant_runtime_contexts.clear()

        monkeypatch.setattr(
            api_main, "_tenant_switch_policy",
            lambda cfg: SimpleNamespace(login_process_timeout_seconds=1),
        )
        monkeypatch.setattr(
            api_main, "_build_tenant_runtime_context",
            lambda registry, tenant_id, run_startup_maintenance=False: new_context,
        )
        monkeypatch.setattr(api_main, "set_active_notifier", lambda notifier: None)
        monkeypatch.setattr(
            "src.core.account_login.stop_all_logins", lambda timeout_seconds=1: None,
        )
        monkeypatch.setattr("src.core.account_login.configure_data_dir", lambda path: None)
        monkeypatch.setattr("src.core.login_manager.configure_data_dir", lambda path: None)

        result = await api_main._switch_tenant_runtime("tenant-b")

        assert result["runtime_tenant_id"] == "tenant-b"
        assert api_main._g_ctx["runtime_tenant_id"] == "tenant-b"
        assert new_context["scheduler"].started == 1
        assert old_context["scheduler"].stopped == 0
        assert old_context["conn"].closed == 0
        assert api_main._tenant_runtime_contexts["tenant-a"]["scheduler"] is old_context["scheduler"]
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(previous_context)
        api_main._tenant_runtime_contexts.clear()
        api_main._tenant_runtime_contexts.update(previous_runtimes)
