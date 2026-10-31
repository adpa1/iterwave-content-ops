from __future__ import annotations

import pytest

from src.core.plan_version import PlanVersionManager
from src.core.task_snapshot import (
    StaleTaskSnapshot,
    build_execution_snapshot,
    sanitize_snapshot,
    validate_execution_snapshot,
)


def test_task_snapshot_removes_credentials_but_keeps_numeric_token_limits():
    safe = sanitize_snapshot({
        "api_key": "secret-api-key",
        "openai_api_key": "secret-openai-key",
        "password": "secret-password",
        "cookies": [{"name": "session", "value": "private"}],
        "max_tokens": 4096,
        "nested": {"client_secret": "private", "temperature": 0.7},
    })
    assert safe == {"max_tokens": 4096, "nested": {"temperature": 0.7}}


def test_execution_snapshot_validates_config_and_plan_versions(cfg, conn, seed):
    round_id = seed.round(status="created")
    snapshot = build_execution_snapshot(
        cfg, conn, round_id=round_id,
        parameters={"round_id": round_id, "topic_count": 5, "platforms": ["xhs"]},
    )
    validate_execution_snapshot(snapshot, cfg, conn)

    cfg.paths.output_dir = str(cfg.paths.output_dir) + "-changed"
    with pytest.raises(StaleTaskSnapshot, match="配置已变化"):
        validate_execution_snapshot(snapshot, cfg, conn)


def test_execution_snapshot_rejects_changed_master_plan(cfg, conn, seed):
    round_id = seed.round(status="created")
    snapshot = build_execution_snapshot(
        cfg, conn, round_id=round_id, parameters={"round_id": round_id},
    )
    manager = PlanVersionManager(cfg, conn)
    plan = manager.load_plan()[0]
    plan["topic_count_per_platform"] = int(plan["topic_count_per_platform"]) + 1
    manager.save_plan(plan)

    with pytest.raises(StaleTaskSnapshot, match="母计划已变化"):
        validate_execution_snapshot(snapshot, cfg, conn)


def test_enqueue_rejects_round_already_based_on_old_plan(cfg, conn, seed):
    round_id = seed.round(status="created")
    manager = PlanVersionManager(cfg, conn)
    original_plan = manager.load_plan()[0]
    manager.record(round_id, original_plan)
    changed_plan = dict(original_plan)
    changed_plan["topic_count_per_platform"] = int(original_plan["topic_count_per_platform"]) + 1
    manager.save_plan(changed_plan)

    with pytest.raises(StaleTaskSnapshot, match="目标轮次已基于旧母计划"):
        build_execution_snapshot(
            cfg, conn, round_id=round_id, parameters={"round_id": round_id},
        )
