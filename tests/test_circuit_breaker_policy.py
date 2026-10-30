"""熔断恢复失败延长时间必须使用账号组配置。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.core.ops.circuit_breaker import CircuitBreakerEngine


class FakeCursor:
    rowcount = 1

    def fetchone(self):
        return ("event-1", "2000-01-01T00:00:00")


class FakeDb:
    def __init__(self):
        self.calls = []
        self.commits = 0

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return FakeCursor()

    def commit(self):
        self.commits += 1


class FakeAdapter:
    async def test_login(self):
        return True

    async def test_lightweight_action(self):
        return False


def test_lightweight_action_failure_uses_configured_extension_minutes():
    platform = SimpleNamespace(value="xhs")
    db = FakeDb()
    cfg = SimpleNamespace(
        circuit_breaker=SimpleNamespace(
            cooldown_minutes=30,
            recovery_failure_extension_minutes=37,
        )
    )
    engine = CircuitBreakerEngine(cfg, db, {platform: FakeAdapter()})

    result = asyncio.run(engine.try_recover(platform))

    assert result["passed"] is False
    assert result["steps"]["test_lightweight_action"] is False
    assert db.calls[-1][1] == ("+37 minutes", "xhs")
    assert db.commits == 1
