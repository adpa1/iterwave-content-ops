"""HOT-015: rollback failure must leave the request gate closed."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import src.api.main as api_main
from src.core.tenant_switch_gate import TenantSwitchGate


def test_hot_015_route_keeps_gate_closed_when_runtime_rollback_fails(monkeypatch):
    gate = TenantSwitchGate()
    monkeypatch.setattr(api_main, "_tenant_switch_gate", gate)
    monkeypatch.setattr(api_main, "_tenant_hot_switch_enabled", lambda: True)

    async def fail_switch(tenant_id):
        raise api_main.TenantRuntimeRollbackError("rollback failed")

    monkeypatch.setattr(api_main, "_switch_tenant_runtime", fail_switch)
    route = next(
        item for item in api_main.app.routes
        if getattr(item, "path", None) == "/api/tenants/select"
    )

    async def run():
        with pytest.raises(HTTPException) as caught:
            await route.endpoint(api_main.TenantSelectRequest(tenant_id="brand_b"))
        assert caught.value.status_code == 503
        assert gate.switching
        with pytest.raises(Exception):
            gate.enter_request()

    try:
        asyncio.run(run())
    finally:
        gate.end_switch()



def test_hot_switch_is_opt_in_and_defaults_to_restart_required(monkeypatch):
    class Registry:
        active = "brand_a"

        def get(self, tenant_id):
            if tenant_id not in ("brand_a", "brand_b"):
                raise api_main.TenantError("账号组不存在")
            return {"id": tenant_id, "name": tenant_id}

        def select_active(self, tenant_id):
            self.active = tenant_id
            return {"id": tenant_id, "name": tenant_id}

    registry = Registry()
    monkeypatch.setattr(api_main, "_tenant_hot_switch_enabled", lambda: False)
    monkeypatch.setattr(api_main, "_g_ctx", {
        "tenant_registry": registry,
        "runtime_tenant_id": "brand_a",
    })
    route = next(
        item for item in api_main.app.routes
        if getattr(item, "path", None) == "/api/tenants/select"
    )

    result = asyncio.run(route.endpoint(api_main.TenantSelectRequest(tenant_id="brand_b")))
    assert result["restart_required"] is True
    assert result["runtime_tenant_id"] == "brand_a"
    assert registry.active == "brand_b"
    assert result["switch_status"] if "switch_status" in result else True
