"""HOT-014: process-level hot-topic status cannot leak across tenant switches."""
from __future__ import annotations

import pytest

from src.core import hot_topic_service


def test_hot_014_reset_runtime_state_clears_idle_state_and_rejects_running_jobs():
    hot_topic_service.reset_for_tests()
    with hot_topic_service._LOCK:
        hot_topic_service._STATE.update(running=False, report={"tenant": "A"}, match={"tenant": "A"})
        hot_topic_service._RESEARCH_STATE.update(running=False, report={"tenant": "A"})

    hot_topic_service.reset_runtime_state()
    assert hot_topic_service.status()["report"] is None
    assert hot_topic_service.status()["match"] is None
    assert hot_topic_service.research_status()["report"] is None

    with hot_topic_service._LOCK:
        hot_topic_service._STATE.update(running=True, report={"tenant": "B"})
    try:
        with pytest.raises(RuntimeError, match="仍在执行"):
            hot_topic_service.reset_runtime_state()
        assert hot_topic_service.status()["running"]
    finally:
        hot_topic_service.reset_for_tests()
