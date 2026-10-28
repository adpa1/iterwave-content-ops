"""运行时配置白名单修改 + 审计（G-13 / G-24）。

为什么不用 yaml.dump 直接改写 config.yaml：
`config/config.yaml` 带大量中文注释，PyYAML 回写会全部丢失注释。
所以采用「白名单补丁 + 覆盖文件」方案：

1. 只允许改白名单里声明的字段（越权 → ConfigNotAllowedError）
2. 改动实时写进内存中的 Config 对象（同一实例被 scheduler / adapters / ops 持有，立即生效）
3. 覆盖值持久化到 `data/config_overrides.json`，下次启动 load_config 之后重新套用
4. 每次改动写 `config_changes`（key / old_value / new_value）留痕

R22 硬锁：维度匹配门 / 可发布性门不在白名单内，且显式拒绝并给出原因，
不允许通过本接口（或任何 UI 入口）关闭。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.config import DEFAULT_JOB_SCHEDULES, JOB_DESCRIPTIONS, Config, validate_cron

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OVERRIDES_FILE = PROJECT_ROOT / "data" / "config_overrides.json"

# R22：这两道门是安全底线，任何入口都不能关
HARD_LOCKED_KEYS = {
    "hot_topics.filters.dimension_match_required": "R22：维度匹配门（账号定位）为强制开启，不可关闭",
    "hot_topics.filters.publishability_required": "R22：可发布性门（红线敏感词）为强制开启，不可关闭",
}

QUOTA_FIELDS = ("passive_replies", "active_comments", "follows")
PLATFORM_KEYS = ("xhs", "douyin", "kuaishou", "wechat_channels")

# 简单标量白名单：路径 → (类型, 最小值, 最大值)
SCALAR_RULES: dict[str, tuple[type, float | None, float | None]] = {
    "rhythm.windows.ops.start_hour": (int, 0, 23),
    "rhythm.windows.ops.end_hour": (int, 0, 23),
    "circuit_breaker.cooldown_minutes": (int, 1, 1440),
    "hot_topics.retention_days": (int, 1, 365),
    "hot_topics.max_rows": (int, 100, 1_000_000),
    "hot_topics.dedup.cross_platform_window_hours": (int, 1, 720),
    "hot_topics.filters.heat_threshold": (int, 0, 10_000_000),
    "hot_topics.filters.heat_filter_enabled": (bool, None, None),
    "hot_topics.filters.time_window_hours": (int, 1, 720),
    "hot_topics.filters.time_filter_enabled": (bool, None, None),
    # 自动闭环（自生长）
    "autogrow.enabled": (bool, None, None),
    "autogrow.apply_feedback": (bool, None, None),
    "autogrow.stop_when_goals_achieved": (bool, None, None),
    "autogrow.topic_count": (int, 1, 50),
    "autogrow.cooldown_hours": (int, 0, 720),
    "autogrow.max_rounds_per_day": (int, 0, 24),
    "autogrow.max_consecutive_failures": (int, 1, 20),
    "autogrow.feedback_max_bias": (int, 0, 50),
    "autogrow.feedback_lookback_rounds": (int, 1, 20),
}


class ConfigNotAllowedError(ValueError):
    """字段不在白名单内（或属于 R22 硬锁）。"""


class ConfigPatchError(ValueError):
    """值类型 / 范围不合法。"""


def _split(key: str) -> list[str]:
    return [p for p in key.split(".") if p]


def _validate(key: str, value: Any) -> Any:
    """按白名单校验并归一化返回值；不合法直接抛错，不做静默兜底。"""
    if key in HARD_LOCKED_KEYS:
        raise ConfigNotAllowedError(HARD_LOCKED_KEYS[key])

    parts = _split(key)
    # rhythm.daily_quotas.<platform>.<field>
    if len(parts) == 4 and parts[:2] == ["rhythm", "daily_quotas"]:
        platform, field = parts[2], parts[3]
        if platform not in PLATFORM_KEYS:
            raise ConfigNotAllowedError(f"未知平台：{platform}")
        if field not in QUOTA_FIELDS:
            raise ConfigNotAllowedError(f"配额字段只允许 {QUOTA_FIELDS}，收到 {field}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigPatchError(f"{key} 必须是整数")
        if value < 0 or value > 10_000:
            raise ConfigPatchError(f"{key} 必须在 0-10000 之间")
        return value

    # scheduler.jobs.<job_id>.enabled | interval_minutes | cron
    if len(parts) == 4 and parts[:2] == ["scheduler", "jobs"]:
        job_id, field = parts[2], parts[3]
        if job_id not in DEFAULT_JOB_SCHEDULES:
            raise ConfigNotAllowedError(f"未知定时任务：{job_id}")
        if field == "enabled":
            if not isinstance(value, bool):
                raise ConfigPatchError(f"{key} 必须是布尔值")
            return value
        if field == "interval_minutes":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigPatchError(f"{key} 必须是整数（分钟）")
            lo = 5 if job_id == "hot_topics_refresh" else 1
            if not lo <= value <= 10080:
                raise ConfigPatchError(f"{key} 必须在 {lo}-10080 分钟之间")
            return value
        if field == "cron":
            if not isinstance(value, str):
                raise ConfigPatchError(f"{key} 必须是 5 段 crontab 字符串")
            try:
                return validate_cron(value)
            except ValueError as e:
                raise ConfigPatchError(str(e)) from e
        raise ConfigNotAllowedError(f"定时任务字段只允许 enabled / interval_minutes / cron，收到 {field}")

    if key == "hot_topics.dedup.cross_platform_similarity":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.5 <= value <= 1.0:
            raise ConfigPatchError(f"{key} 必须是 0.5-1.0 之间的数字")
        return float(value)

    rule = SCALAR_RULES.get(key)
    if rule is None:
        raise ConfigNotAllowedError(f"字段不在可修改白名单内：{key}")
    typ, lo, hi = rule
    if typ is bool:
        if not isinstance(value, bool):
            raise ConfigPatchError(f"{key} 必须是布尔值")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigPatchError(f"{key} 必须是数字")
    if lo is not None and value < lo:
        raise ConfigPatchError(f"{key} 不能小于 {lo}")
    if hi is not None and value > hi:
        raise ConfigPatchError(f"{key} 不能大于 {hi}")
    return int(value)


def _get_value(cfg: Config, key: str) -> Any:
    node: Any = cfg
    for part in _split(key):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            node = getattr(node, part, None)
        if node is None:
            return None
    return node


def _counterpart(key: str) -> str | None:
    """interval_minutes 与 cron 互斥：设置其一时要清掉另一个。"""
    parts = _split(key)
    if len(parts) == 4 and parts[:2] == ["scheduler", "jobs"]:
        other = {"interval_minutes": "cron", "cron": "interval_minutes"}.get(parts[3])
        if other:
            return ".".join(parts[:3] + [other])
    return None


def _set_value(cfg: Config, key: str, value: Any) -> None:
    other = _counterpart(key)
    if other:
        job = cfg.scheduler.jobs[_split(key)[2]]
        # 直接改字段（绕过 pydantic 赋值校验的中间态），两者始终只保留一个
        object.__setattr__(job, _split(other)[3], None)
        object.__setattr__(job, _split(key)[3], value)
        return
    parts = _split(key)
    node: Any = cfg
    for part in parts[:-1]:
        if isinstance(node, dict):
            if part not in node:
                node[part] = {}
            node = node[part]
        else:
            node = getattr(node, part)
    last = parts[-1]
    if isinstance(node, dict):
        node[last] = value
    else:
        setattr(node, last, value)


def load_overrides() -> dict[str, Any]:
    """读取持久化的覆盖值；文件不存在 / 损坏 → 空（不猜测、不补默认）。"""
    if not OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.error("config_overrides.json 读取失败，本次不套用覆盖值: %s", e)
        return {}


def _save_overrides(overrides: dict[str, Any]) -> None:
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def apply_overrides(cfg: Config, overrides: dict[str, Any]) -> list[str]:
    """启动时套用覆盖值。校验失败的条目跳过并记警告（不静默改写业务值）。"""
    applied: list[str] = []
    for key, value in overrides.items():
        try:
            normalized = _validate(key, value)
        except ValueError as e:
            logger.warning("覆盖值 %s 校验失败，已跳过：%s", key, e)
            continue
        _set_value(cfg, key, normalized)
        applied.append(key)
    return applied


def scheduler_jobs_view(cfg: Config) -> dict[str, dict[str, Any]]:
    return {
        job_id: {
            "description": JOB_DESCRIPTIONS.get(job_id, job_id),
            "enabled": j.enabled,
            "interval_minutes": j.interval_minutes,
            "cron": j.cron,
            "schedule": j.describe(),
        }
        for job_id, j in cfg.scheduler.jobs.items()
    }


def changed_job_ids(keys) -> set[str]:
    """从修改过的配置键里找出需要重排的定时任务。"""
    out: set[str] = set()
    for k in keys:
        parts = _split(k)
        if len(parts) == 4 and parts[:2] == ["scheduler", "jobs"]:
            out.add(parts[2])
        if k == "hot_topics.enabled":
            out.add("hot_topics_refresh")
    return out


def current_config_view(cfg: Config, conn) -> dict[str, Any]:
    """Settings 页展示 + 编辑所需的白名单字段当前值。"""
    enabled = cfg.platforms.enabled_keys()
    quotas: dict[str, dict[str, int | None]] = {}
    for p in enabled:
        quota = cfg.rhythm.daily_quotas.get(p)
        quotas[p] = {
            f: (getattr(quota, f, None) if quota is not None else None)
            for f in QUOTA_FIELDS
        }
    return {
        "editable": {
            "daily_quotas": quotas,
            "ops_window": {
                "start_hour": cfg.rhythm.windows.get("ops").start_hour
                if cfg.rhythm.windows.get("ops") else None,
                "end_hour": cfg.rhythm.windows.get("ops").end_hour
                if cfg.rhythm.windows.get("ops") else None,
            },
            "cooldown_minutes": cfg.circuit_breaker.cooldown_minutes,
            "scheduler_jobs": scheduler_jobs_view(cfg),
            "autogrow": cfg.autogrow.model_dump(),
            "hot_topics_storage": {
                "retention_days": cfg.hot_topics.retention_days,
                "max_rows": cfg.hot_topics.max_rows,
                "cross_platform_similarity": cfg.hot_topics.dedup.cross_platform_similarity,
                "cross_platform_window_hours": cfg.hot_topics.dedup.cross_platform_window_hours,
            },
            "hot_topics": {
                "heat_threshold": cfg.hot_topics.filters.get("heat_threshold"),
                "heat_filter_enabled": cfg.hot_topics.filters.get(
                    "heat_filter_enabled", True
                ),
                "time_window_hours": cfg.hot_topics.filters.get("time_window_hours"),
                "time_filter_enabled": cfg.hot_topics.filters.get(
                    "time_filter_enabled", True
                ),
            },
        },
        # R22 两道硬锁：只展示、不可改；前端无关闭入口
        "locked": {
            "dimension_match_required": True,
            "publishability_required": True,
        },
        # 账号画像（Assets「个人观点与画像」tab 展示用，只读）
        "readonly": {
            "persona_keywords": list(cfg.ultimate_goal.persona_keywords),
            "forbidden_directions": list(cfg.ultimate_goal.forbidden_directions),
            "redline_keywords_count": len(cfg.ultimate_goal.redline_keywords),
            # —— 本地知识库（Settings 页「连接本地知识库」panel）——
            "kb_path": str(cfg.topic_kb.path),
            "kb_type": cfg.topic_kb.type,
            "kb_table": cfg.topic_kb.table_name,
            "kb_field_mapping": cfg.topic_kb.field_mapping,
            # —— LLM（Settings 页「主模型与备用」panel）——
            "llm_provider": cfg.llm.provider,
            "llm_model": cfg.llm.model,
            "llm_fallback_model": getattr(cfg.llm, "fallback_model", None),
            "llm_has_api_key": bool(__import__("os").environ.get(cfg.llm.api_key_env)),
            # —— 平台连接状态（只读，登录态文件 + enabled 开关）——
            "platforms": {
                k: {
                    "enabled": v.get("enabled") if isinstance(v, dict) else getattr(v, "enabled", None),
                    "account_key": v.get("account_key") if isinstance(v, dict) else getattr(v, "account_key", None),
                    "base_url": v.get("base_url") if isinstance(v, dict) else getattr(v, "base_url", None),
                }
                for k, v in cfg.platforms.model_dump().items()
                if k in ("xhs", "douyin", "kuaishou", "wechat_channels")
            },
        },
        "overrides_file": str(OVERRIDES_FILE),
        "overrides": load_overrides(),
        "audit": [
            dict(r)
            for r in conn.execute(
                "SELECT id, key, old_value, new_value, changed_at "
                "FROM config_changes ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ],
    }


def patch_config(cfg: Config, conn, updates: dict[str, Any]) -> dict[str, Any]:
    """白名单批量修改：校验 → 改内存 → 持久化覆盖 → 写审计。

    任一条不合法 → 整体拒绝（不做部分成功），避免半套配置生效。
    """
    if not updates:
        raise ConfigPatchError("updates 不能为空")

    normalized: dict[str, Any] = {}
    for key, value in updates.items():
        normalized[key] = _validate(key, value)

    # 时段窗口 start < end（R5 本地时区语义，不能跨越成空窗口）
    if (
        "rhythm.windows.ops.start_hour" in normalized
        or "rhythm.windows.ops.end_hour" in normalized
    ):
        start = normalized.get(
            "rhythm.windows.ops.start_hour",
            _get_value(cfg, "rhythm.windows.ops.start_hour"),
        )
        end = normalized.get(
            "rhythm.windows.ops.end_hour",
            _get_value(cfg, "rhythm.windows.ops.end_hour"),
        )
        if start is not None and end is not None and start >= end:
            raise ConfigPatchError("运营时段 start_hour 必须小于 end_hour")

    changes: list[dict[str, Any]] = []
    overrides = load_overrides()
    for key, value in normalized.items():
        old = _get_value(cfg, key)
        if old == value:
            continue
        _set_value(cfg, key, value)
        overrides[key] = value
        other = _counterpart(key)
        if other:
            overrides.pop(other, None)
        conn.execute(
            "INSERT INTO config_changes (key, old_value, new_value, changed_at) "
            "VALUES (?, ?, ?, ?)",
            (
                key,
                None if old is None else str(old),
                str(value),
                # R5：本地时区（表默认 CURRENT_TIMESTAMP 是 UTC，必须显式覆盖）
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        changes.append({"key": key, "old": old, "new": value})

    if changes:
        _save_overrides(overrides)
        conn.commit()
        logger.info("⚙️ 配置已修改 %d 项：%s", len(changes), [c["key"] for c in changes])
    return {"changed": changes, "applied": normalized, "noop": len(changes) == 0}
