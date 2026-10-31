"""队列任务执行快照：固定非敏感配置指纹、母计划版本和显式执行参数。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


_SENSITIVE_KEYS = {
    "api_key", "apikey", "secret", "client_secret", "password", "cookie", "cookies",
    "authorization", "access_token", "refresh_token", "auth_token", "session_token",
    "private_key", "credential", "credentials",
}


class StaleTaskSnapshot(RuntimeError):
    """排队后非敏感配置或母计划发生变化，拒绝使用新配置静默执行。"""


def _safe_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return not (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_api_key")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
        or normalized.endswith("_cookie")
        or normalized.endswith("_token")
    )


def sanitize_snapshot(value: Any) -> Any:
    """递归移除凭据字段；不复制 API Key、Cookie、密码或会话令牌。"""
    if isinstance(value, dict):
        return {
            str(key): sanitize_snapshot(item)
            for key, item in value.items()
            if _safe_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_snapshot(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def config_fingerprint(cfg) -> str:
    raw = cfg.model_dump(mode="json")
    safe = sanitize_snapshot(raw)
    canonical = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_execution_snapshot(cfg, conn, *, round_id: int, parameters: dict[str, Any]) -> dict[str, Any]:
    from .plan_version import PlanVersionManager

    plan, signature, updated_at, source = PlanVersionManager(cfg, conn).load_plan()
    round_row = conn.execute(
        "SELECT plan_signature FROM spiral_rounds WHERE id = ?", (round_id,),
    ).fetchone()
    round_signature = round_row["plan_signature"] if round_row else None
    if round_signature and round_signature != signature:
        raise StaleTaskSnapshot("目标轮次已基于旧母计划；请先确认并更新轮次，再重新提交生产任务")
    round_plan = None
    if round_signature:
        row = conn.execute(
            "SELECT plan_snapshot FROM signature_history WHERE round_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (round_id,),
        ).fetchone()
        if row:
            try:
                round_plan = json.loads(row["plan_snapshot"])
            except (TypeError, ValueError):
                round_plan = None
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_fingerprint": config_fingerprint(cfg),
        "plan_signature": signature,
        "plan_updated_at": updated_at,
        "plan_source": source,
        "round_plan_signature": round_signature,
        "round_plan_snapshot": sanitize_snapshot(round_plan) if round_plan else None,
        "parameters": sanitize_snapshot(parameters),
    }


def validate_execution_snapshot(snapshot: dict[str, Any], cfg, conn) -> None:
    """配置或母计划发生变化时 fail closed，要求用户确认后重新入队。"""
    if int(snapshot.get("schema_version") or 0) != 1:
        raise StaleTaskSnapshot("任务执行快照版本不受支持，请重新提交任务")
    expected_config = str(snapshot.get("config_fingerprint") or "")
    if not expected_config or config_fingerprint(cfg) != expected_config:
        raise StaleTaskSnapshot("任务入队后账号组配置已变化；为避免使用新配置误执行，任务已停止，请确认配置后重新提交")
    expected_plan = str(snapshot.get("plan_signature") or "")
    if expected_plan:
        from .plan_version import PlanVersionManager
        _, current_signature, _, _ = PlanVersionManager(cfg, conn).load_plan()
        if current_signature != expected_plan:
            raise StaleTaskSnapshot("任务入队后母计划已变化；为避免按错误计划生产，任务已停止，请确认计划后重新提交")
