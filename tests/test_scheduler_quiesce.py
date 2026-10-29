"""SchedulerEngine 的热切换排空语义测试。"""
from __future__ import annotations

import asyncio

from src.core.scheduler import SchedulerEngine


class FakeAsyncScheduler:
    def __init__(self):
        self.running = True
        self.paused = False
        self.resumed = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.resumed = True

    def shutdown(self, wait=False):
        self.running = False


def make_engine():
    engine = SchedulerEngine.__new__(SchedulerEngine)
    engine.scheduler = FakeAsyncScheduler()
    engine._active_job_count = 0
    engine.engine_key = "/tmp/test-tenant.db"
    return engine


def test_quiesce_waits_for_active_job_before_shutdown():
    engine = make_engine()
    engine._active_job_count = 1
    shutdown_calls = []
    engine.shutdown = lambda: shutdown_calls.append(True)

    async def run():
        async def finish_job():
            await asyncio.sleep(0.08)
            engine._active_job_count -= 1

        job = asyncio.create_task(finish_job())
        drained = await engine.quiesce(timeout_seconds=1)
        await job
        assert drained
        assert engine.scheduler.paused
        assert engine._active_job_count == 0
        assert shutdown_calls == [True]

    asyncio.run(run())


def test_quiesce_resumes_scheduler_if_job_does_not_drain():
    engine = make_engine()
    engine._active_job_count = 1
    shutdown_calls = []
    engine.shutdown = lambda: shutdown_calls.append(True)

    async def run():
        drained = await engine.quiesce(timeout_seconds=0.01)
        assert not drained
        assert engine.scheduler.paused
        assert engine.scheduler.resumed
        assert shutdown_calls == []

    asyncio.run(run())
