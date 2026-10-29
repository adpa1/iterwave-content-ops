"""账号组运行时切换请求闸门测试。"""
from __future__ import annotations

import asyncio
import pytest

from src.core.tenant_switch_gate import TenantSwitchGate, TenantSwitchInProgress


def test_switch_waits_for_inflight_requests_and_reopens_gate() -> None:
    gate = TenantSwitchGate()
    gate.enter_request()

    async def run() -> None:
        async def finish_request() -> None:
            await asyncio.sleep(0.03)
            gate.leave_request()

        task = asyncio.create_task(finish_request())
        await gate.begin_switch(timeout_seconds=1)
        await task
        assert gate.switching
        assert gate.active_requests == 0
        with pytest.raises(TenantSwitchInProgress):
            gate.enter_request()
        gate.end_switch()
        gate.enter_request()
        gate.leave_request()

    asyncio.run(run())


def test_switch_timeout_reopens_gate_without_leaking_state() -> None:
    gate = TenantSwitchGate()
    gate.enter_request()

    async def run() -> None:
        with pytest.raises(TimeoutError):
            await gate.begin_switch(timeout_seconds=0.01)
        assert not gate.switching
        gate.enter_request()
        gate.leave_request()
        gate.leave_request()

    asyncio.run(run())


def test_only_one_switch_can_own_gate() -> None:
    gate = TenantSwitchGate()

    async def run() -> None:
        await gate.begin_switch()
        with pytest.raises(TenantSwitchInProgress):
            await gate.begin_switch()
        gate.end_switch()

    asyncio.run(run())
