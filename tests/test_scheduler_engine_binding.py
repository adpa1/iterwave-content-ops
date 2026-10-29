"""Scheduler jobs must remain bound to their tenant engine, never a mutable global active engine."""
from __future__ import annotations

import asyncio

from src.core.scheduler import _ENGINES, _run_publish_polling, _run_data_fetch, _engine_for


class _DummyEngine:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def _job_publish_polling(self) -> None:
        self.calls.append(("publish_polling",))

    async def _job_data_fetch(self, draft_id: str, attempt: int) -> None:
        self.calls.append(("data_fetch", draft_id, attempt))


def test_job_wrapper_dispatches_to_engine_key_not_last_started_engine(monkeypatch) -> None:
    tenant_a = _DummyEngine()
    tenant_b = _DummyEngine()
    monkeypatch.setitem(_ENGINES, "/tenant-a/data.db", tenant_a)
    monkeypatch.setitem(_ENGINES, "/tenant-b/data.db", tenant_b)

    asyncio.run(_run_publish_polling("/tenant-a/data.db"))
    asyncio.run(_run_publish_polling("/tenant-b/data.db"))
    asyncio.run(_run_data_fetch("/tenant-a/data.db", draft_id="draft-A", attempt=2))

    assert tenant_a.calls == [("publish_polling",), ("data_fetch", "draft-A", 2)]
    assert tenant_b.calls == [("publish_polling",)]


def test_job_with_missing_or_stale_engine_key_fails_closed(monkeypatch, caplog) -> None:
    monkeypatch.setattr("src.core.scheduler._ENGINES", {})
    assert _engine_for(None) is None
    assert _engine_for("/tenant-that-is-not-running/data.db") is None
    assert "拒绝执行" in caplog.text
    assert "跳过 job" in caplog.text
