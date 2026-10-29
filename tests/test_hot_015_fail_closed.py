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
