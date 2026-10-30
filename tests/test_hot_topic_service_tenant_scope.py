from __future__ import annotations

from types import SimpleNamespace

import src.api.main as api_main
from src.core import hot_topic_service


def test_hot_topic_status_is_visible_only_to_its_tenant():
    hot_topic_service.reset_for_tests()
    try:
        with hot_topic_service._LOCK:
            hot_topic_service._STATE.update(
                running=True,
                tenant_id="tenant-a",
                started_at="2026-10-30T10:00:00",
                progress={"stage": "collecting", "percent": 40},
            )

        own = api_main._tenant_scoped_hot_topic_status(
            hot_topic_service.status(), "tenant-a",
        )
        other = api_main._tenant_scoped_hot_topic_status(
            hot_topic_service.status(), "tenant-b",
        )

        assert own["running"] is True
        assert own["tenant_id"] == "tenant-a"
        assert other["running"] is False
        assert other["tenant_id"] is None
        assert other["progress"] is None
    finally:
        hot_topic_service.reset_for_tests()


def test_hot_topic_service_reports_busy_tenant_instead_of_misrouting_work(monkeypatch):
    class FakeThread:
        def __init__(self, *, target, args=(), kwargs=None, name=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}
            self.name = name
            self.daemon = daemon

        def start(self):
            return None

    monkeypatch.setattr(hot_topic_service.threading, "Thread", FakeThread)
    hot_topic_service.reset_for_tests()
    try:
        first = hot_topic_service.trigger_background(
            SimpleNamespace(), tenant_id="tenant-a",
        )
        assert first["started"] is True

        with hot_topic_service._LOCK:
            hot_topic_service._STATE.update(running=True, tenant_id="tenant-a")

        second = hot_topic_service.trigger_background(
            SimpleNamespace(), tenant_id="tenant-b",
        )
        research = hot_topic_service.trigger_research(
            SimpleNamespace(), tenant_id="tenant-b",
        )

        assert second["started"] is False
        assert second["tenant_id"] == "tenant-a"
        assert research["started"] is False
        assert research["tenant_id"] == "tenant-a"
        assert "正在执行" in research["reason"]
    finally:
        hot_topic_service.reset_for_tests()
