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
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.config import DEFAULT_JOB_SCHEDULES, JOB_DESCRIPTIONS, Config, validate_cron
from src.core.redact import redact_path

logger = logging.getLogger(__name__)

# 综合热榜覆盖的平台（新增来源时不指定即默认全选）
AGGREGATE_PLATFORMS = ("weibo", "douyin", "zhihu", "toutiao", "bilibili")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 默认跟随 cfg.paths.data_dir（不写死绝对路径）；测试可 monkeypatch 本变量指向临时文件
OVERRIDES_FILE: Optional[Path] = None

# R22：这两道门是安全底线，任何入口都不能关
HARD_LOCKED_KEYS = {
    "hot_topics.filters.dimension_match_required": "R22：维度匹配门（账号定位）为强制开启，不可关闭",
    "hot_topics.filters.publishability_required": "R22：可发布性门（红线敏感词）为强制开启，不可关闭",
}

QUOTA_FIELDS = ("passive_replies", "active_comments", "follows")
PLATFORM_KEYS = ("xhs", "douyin", "kuaishou", "wechat_channels")
KB_TYPES = ("auto", "sqlite", "csv", "json", "jsonl", "xlsx", "rss")
KB_MAPPING_FIELDS = ("source", "title", "summary", "link", "date", "dimension")
# 三模式评分权重（R11）：改动后每个模式权重和必须仍 = 100
SCORING_MODES = ("cold_start", "sample_accumulating", "stable_operation")
SCORING_WEIGHT_FIELDS = (
    "position_match", "history_performance", "timeliness", "gap_weight", "freshness",
)
RHYTHM_INTERVAL_FIELDS = (
    "passive_reply_base_seconds", "passive_reply_jitter_seconds",
    "active_comment_base_seconds", "active_comment_jitter_seconds",
    "follow_base_seconds", "follow_jitter_seconds",
)

# 字符串白名单：路径 → (最小长度, 最大长度)
STRING_RULES: dict[str, tuple[int, int]] = {
    "topic_kb.path": (1, 512),
    "topic_kb.table_name": (1, 64),
    "topic_kb.default_dimension": (1, 20),
    "render.font_path": (0, 512),
    "render.card_tag_fallback": (1, 20),
    "llm.provider": (1, 32),
    "llm.model": (1, 128),
    "llm.fallback_model": (0, 128),
}

# 字符串列表白名单（账号画像 / 红线词库）：路径 → 最大条数
LIST_RULES: dict[str, int] = {
    "ultimate_goal.persona_keywords": 30,
    "ultimate_goal.forbidden_directions": 30,
    # R22：红线词库本身可增删（门禁开关仍硬锁），单条最长 20 字
    "ultimate_goal.redline_keywords": 200,
}

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
    "llm.enabled": (bool, None, None),
    # 通知通道开关（页面只在运营中心提供开关；也可从这里改）
    "notifications.channels.macos.enabled": (bool, None, None),
    "notifications.channels.wecom.enabled": (bool, None, None),
    "notifications.channels.feishu.enabled": (bool, None, None),
    # R8：每个超时只作用于自己的阶段，改一个不影响另一个
    "timeouts.round_timeout_hours": (int, 1, 720),
    "timeouts.awaiting_publish_timeout_hours": (int, 1, 1440),
    "timeouts.publish_confirm_timeout_hours": (int, 1, 1440),
    "timeouts.publish_confirm_grace_hours": (int, 0, 720),
    "timeouts.collecting_metrics_timeout_hours": (int, 1, 720),
    "timeouts.metrics_initial_delay_hours": (int, 0, 720),
    "timeouts.goal_max_rounds": (int, 1, 500),
    "timeouts.round_min_interval_hours": (int, 0, 168),
    # 存草稿时段窗口（本地时区，R5）
    "rhythm.windows.draft_save.start_hour": (int, 0, 23),
    "rhythm.windows.draft_save.end_hour": (int, 0, 23),
    # 热点采集总开关
    "hot_topics.enabled": (bool, None, None),
    # LLM 调用参数（base_url 走单独 URL 校验）
    "llm.temperature": (float, 0.0, 2.0),
    "llm.timeout_seconds": (int, 5, 300),
    # R23 数据保留期限（天）
    "data_retention.raw_response_days": (int, 7, 3650),
    "data_retention.screenshot_days": (int, 1, 3650),
    "data_retention.ops_logs_days": (int, 7, 3650),
    "data_retention.ai_calls_detail_days": (int, 7, 3650),
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

    # —— 三模式权重 / 时效半衰期（R11/R12，可在线调，但每个模式仍须合计 100）——
    if len(parts) == 3 and parts[0] == "scoring" and parts[1] in SCORING_MODES \
            and parts[2] in SCORING_WEIGHT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigPatchError(f"{key} 必须是整数（0-100）")
        if not 0 <= value <= 100:
            raise ConfigPatchError(f"{key} 必须在 0-100 之间")
        return value

    if len(parts) == 4 and parts[:2] == ["scoring", "timeliness_half_life_days"] \
            and parts[2] in ("news", "evergreen"):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
            raise ConfigPatchError(f"{key} 必须是 1-3650 的整数（天）")
        return value

    # R12：口径参数（活跃维度判定 / 90 天窗口 / 收缩估计 m）
    if key in ("scoring.active_dimension_min_uses", "scoring.active_dimension_window_days",
               "scoring.shrinkage_m"):
        lo, hi = {
            "scoring.active_dimension_min_uses": (1, 10),
            "scoring.active_dimension_window_days": (7, 365),
            "scoring.shrinkage_m": (1, 50),
        }[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ConfigPatchError(f"{key} 必须是 {lo}-{hi} 的整数")
        return value

    # —— 节奏随机间隔（秒）——
    if len(parts) == 3 and parts[:2] == ["rhythm", "intervals"] \
            and parts[2] in RHYTHM_INTERVAL_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3600:
            raise ConfigPatchError(f"{key} 必须是 0-3600 的整数（秒）")
        return value

    # —— 平台 base_url（一般不用改，环境/代理变化时可调）——
    if len(parts) == 3 and parts[0] == "platforms" and parts[2] == "base_url":
        if parts[1] not in PLATFORM_KEYS:
            raise ConfigNotAllowedError(f"未知平台：{parts[1]}")
        if not isinstance(value, str) or not value.strip().startswith(("http://", "https://")):
            raise ConfigPatchError(f"{key} 必须是 http(s) URL")
        if len(value) > 256:
            raise ConfigPatchError(f"{key} 最长 256 字符")
        return value.strip()

    # —— LLM base_url（按量付费 / plan 通道切换时可调）——
    if key == "llm.base_url":
        if not isinstance(value, str) or not value.strip().startswith(("http://", "https://")):
            raise ConfigPatchError(f"{key} 必须是 http(s) URL")
        if len(value) > 256:
            raise ConfigPatchError(f"{key} 最长 256 字符")
        return value.rstrip("/")

    # —— 平台登录态 key（换号/多账号时改，决定 login_state 文件读哪个）——
    if len(parts) == 3 and parts[0] == "platforms" and parts[2] == "account_key":
        if parts[1] not in PLATFORM_KEYS:
            raise ConfigNotAllowedError(f"未知平台：{parts[1]}")
        if not isinstance(value, str):
            raise ConfigPatchError(f"{key} 必须是字符串")
        v = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", v):
            raise ConfigPatchError(f"{key} 只能是字母/数字/下划线/连字符，1-64 字符")
        return v

    # —— 企微/飞书 webhook 环境变量名（URL 本身仍只存 .env，R23）——
    if (
        len(parts) == 4
        and parts[:2] == ["notifications", "channels"]
        and parts[3] == "webhook_url_env"
    ):
        if parts[2] not in ("wecom", "feishu"):
            raise ConfigPatchError(f"{key} 只支持 wecom / feishu")
        if not isinstance(value, str):
            raise ConfigPatchError(f"{key} 必须是环境变量名")
        v = value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", v):
            raise ConfigPatchError(f"{key} 必须是合法环境变量名（大写字母/数字/下划线，1-64）")
        return v

    if key == "hot_topics.sources":
        if not isinstance(value, list) or len(value) > 20:
            raise ConfigPatchError("热点源必须是数组（最多 20 条）")
        cleaned: list[dict] = []
        for item in value:
            if not isinstance(item, dict):
                raise ConfigPatchError("热点源每一条必须是对象")
            stype = item.get("type", "aggregate")
            if stype not in ("aggregate", "rss", "json"):
                raise ConfigPatchError("热点源 type 只能是 aggregate / rss / json")
            url = str(item.get("url") or "").strip()
            if stype != "aggregate" and not url.startswith(("https://", "http://")):
                raise ConfigPatchError("自定义热点源 url 必须是 http(s) 地址")
            if stype == "aggregate" and url and not url.startswith(("https://", "http://")):
                raise ConfigPatchError("综合热榜 url 必须是 http(s) 地址")
            name = str(item.get("name") or "").strip()
            if not name or len(name) > 60:
                raise ConfigPatchError("热点源名称必填且不超过 60 字")
            row: dict = {"name": name, "type": stype, "url": url,
                         "enabled": bool(item.get("enabled", True))}
            if stype == "aggregate":
                platforms = item.get("platforms")
                if not isinstance(platforms, list) or not platforms:
                    single = str(item.get("platform") or "").strip()
                    platforms = [single] if single else list(AGGREGATE_PLATFORMS)
                row["platforms"] = [str(p).strip() for p in platforms if str(p).strip()][:10] \
                    or list(AGGREGATE_PLATFORMS)
            else:
                row["platform"] = str(item.get("platform") or name)[:40]
            cleaned.append(row)
        return cleaned

    if key == "topic_kb.type":
        if not isinstance(value, str) or value not in KB_TYPES:
            raise ConfigPatchError(f"{key} 只能是 {' / '.join(KB_TYPES)}")
        return value

    if key == "topic_kb.field_mapping":
        if not isinstance(value, dict):
            raise ConfigPatchError(f"{key} 必须是 {{通用字段: 原始列名}} 的对象")
        out_map: dict[str, str] = {}
        for field_name, column in value.items():
            if field_name not in KB_MAPPING_FIELDS:
                raise ConfigPatchError(
                    f"{key} 的通用字段只能是 {' / '.join(KB_MAPPING_FIELDS)}，收到 {field_name}"
                )
            if not isinstance(column, str) or not column.strip():
                raise ConfigPatchError(f"{key}.{field_name} 必须是非空字符串")
            out_map[field_name] = column.strip()
        return out_map

    # ===== 账号画像：模式 / 一句话定位 / 平台画像 / 平台协同 =====
    if key == "ultimate_goal.goal_mode":
        from .persona import GOAL_MODES
        if value not in GOAL_MODES:
            raise ConfigPatchError(f"goal_mode 只能是 {' / '.join(GOAL_MODES)}")
        return value
    if key == "ultimate_goal.positioning":
        if not isinstance(value, str) or len(value.strip()) > 200:
            raise ConfigPatchError("账号定位必须是不超过 200 字的文字")
        return value.strip()
    if len(parts) == 4 and parts[:2] == ["ultimate_goal", "platform_personas"]:
        from .persona import PLATFORM_ROLES
        platform, field = parts[2], parts[3]
        if platform not in PLATFORM_KEYS:
            raise ConfigNotAllowedError(f"未知平台：{platform}")
        if field == "inherit_global":
            if not isinstance(value, bool):
                raise ConfigPatchError(f"{key} 必须是布尔值")
            return value
        if field in ("account_name", "positioning", "audience", "role"):
            limit = {"account_name": 40, "positioning": 200, "audience": 200, "role": 20}[field]
            if not isinstance(value, str) or len(value.strip()) > limit:
                raise ConfigPatchError(f"{key} 必须是不超过 {limit} 字的文字")
            if field == "role" and value.strip() not in PLATFORM_ROLES:
                raise ConfigPatchError(f"平台角色只能是 {' / '.join(r for r in PLATFORM_ROLES if r)} 或留空")
            return value.strip()
        if field in ("persona_keywords", "forbidden_directions", "extra_redlines"):
            cap = 50 if field == "extra_redlines" else 30
            if not isinstance(value, list):
                raise ConfigPatchError(f"{key} 必须是字符串数组")
            out_l: list[str] = []
            for item in value:
                if not isinstance(item, str):
                    raise ConfigPatchError(f"{key} 的每一项都必须是字符串")
                t = item.strip()
                if not t:
                    continue
                if len(t) > 20:
                    raise ConfigPatchError(f"{key} 单项最长 20 字：{t}")
                if t not in out_l:
                    out_l.append(t)
            if len(out_l) > cap:
                raise ConfigPatchError(f"{key} 最多 {cap} 项")
            return out_l
        raise ConfigNotAllowedError(f"平台画像字段不允许修改：{field}")
    if key == "ultimate_goal.platform_links":
        from .persona import LINK_RELATIONS
        if not isinstance(value, list) or len(value) > 12:
            raise ConfigPatchError("平台协同关系必须是数组，最多 12 条")
        links: list[dict] = []
        for item in value:
            if not isinstance(item, dict):
                raise ConfigPatchError("每条协同关系必须包含 source / target / relation")
            src, tgt = item.get("source"), item.get("target")
            rel = item.get("relation") or "traffic"
            note = str(item.get("note") or "").strip()[:100]
            if src not in PLATFORM_KEYS or tgt not in PLATFORM_KEYS:
                raise ConfigPatchError("协同关系的平台必须是 xhs / douyin / kuaishou / wechat_channels")
            if src == tgt:
                raise ConfigPatchError("协同关系不能指向同一个平台")
            if rel not in LINK_RELATIONS:
                raise ConfigPatchError(f"relation 只能是 {' / '.join(LINK_RELATIONS)}")
            links.append({"source": src, "target": tgt, "relation": rel, "note": note})
        return links

    if key in STRING_RULES:
        lo, hi = STRING_RULES[key]
        if not isinstance(value, str):
            raise ConfigPatchError(f"{key} 必须是字符串")
        text = value.strip()
        if not lo <= len(text) <= hi:
            raise ConfigPatchError(f"{key} 长度必须在 {lo}-{hi} 之间")
        return text

    if key in LIST_RULES:
        if not isinstance(value, list):
            raise ConfigPatchError(f"{key} 必须是字符串数组")
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ConfigPatchError(f"{key} 的每一项都必须是字符串")
            text = item.strip()
            if not text:
                continue
            if len(text) > 20:
                raise ConfigPatchError(f"{key} 单项最长 20 字：{text}")
            if text not in out:
                out.append(text)
        if len(out) > LIST_RULES[key]:
            raise ConfigPatchError(f"{key} 最多 {LIST_RULES[key]} 项")
        return out

    # platforms.<platform>.enabled
    if len(parts) == 3 and parts[0] == "platforms" and parts[2] == "enabled":
        if parts[1] not in PLATFORM_KEYS:
            raise ConfigNotAllowedError(f"未知平台：{parts[1]}")
        if not isinstance(value, bool):
            raise ConfigPatchError(f"{key} 必须是布尔值")
        return value

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
    return float(value) if typ is float else int(value)


def _jsonable(v: Any) -> Any:
    """pydantic 模型 / 模型列表 → 普通 dict（审计与 API 返回用）。"""
    if hasattr(v, "model_dump"):
        return v.model_dump()
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


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
    parts0 = _split(key)
    if len(parts0) == 4 and parts0[:2] == ["ultimate_goal", "platform_personas"]:
        from .config import PlatformPersonaConfig
        from .persona import platform_persona
        pp = platform_persona(cfg, parts0[2]) or PlatformPersonaConfig()
        setattr(pp, parts0[3], value)
        cfg.ultimate_goal.platform_personas[parts0[2]] = pp
        return
    if key == "ultimate_goal.platform_links":
        from .config import PlatformLinkConfig
        cfg.ultimate_goal.platform_links = [PlatformLinkConfig(**l) for l in value]
        return
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


def overrides_path(cfg: Config) -> Path:
    """覆盖值落在数据目录（跟着 cfg.paths.data_dir 走，不写死绝对路径）。"""
    if OVERRIDES_FILE is not None:
        return Path(OVERRIDES_FILE)
    return Path(cfg.paths.data_dir) / "config_overrides.json"


def load_overrides(cfg: Config) -> dict[str, Any]:
    """读取持久化的覆盖值；文件不存在 / 损坏 → 空（不猜测、不补默认）。"""
    path = overrides_path(cfg)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.error("config_overrides.json 读取失败，本次不套用覆盖值: %s", e)
        return {}


def _save_overrides(cfg: Config, overrides: dict[str, Any]) -> None:
    path = overrides_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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


def llm_channel(base_url: str) -> str:
    """按 base_url 判定计费通道：plan（Agent Plan 专属通道）/ metered（按量付费）/ custom。"""
    url = (base_url or "").rstrip("/")
    if url.endswith("/api/plan/v3"):
        return "plan"
    if url.endswith("/api/v3"):
        return "metered"
    return "custom"


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
            "positioning": cfg.ultimate_goal.positioning,
            "goal_mode": cfg.ultimate_goal.goal_mode,
            # —— 本地知识库（Settings 页只读展示；路径脱敏，来源在 Assets 页管理）——
            "kb_path": redact_path(cfg.topic_kb.path),
            "kb_type": cfg.topic_kb.type,
            "kb_table": cfg.topic_kb.table_name,
            "kb_field_mapping": cfg.topic_kb.field_mapping,
            "kb_default_dimension": cfg.topic_kb.default_dimension,
            "render_font_path": redact_path(cfg.render.font_path or ""),
            "render_card_tag_fallback": cfg.render.card_tag_fallback,
            # —— LLM（Settings 页「主模型与备用」panel）——
            "llm_provider": cfg.llm.provider,
            "llm_model": cfg.llm.model,
            "llm_fallback_model": getattr(cfg.llm, "fallback_model", None),
            "llm_base_url": cfg.llm.base_url,
            # 计费通道：由 base_url 推导（前端下拉选择按量付费 / 专属 Agent Plan）
            "llm_channel": llm_channel(cfg.llm.base_url),
            "llm_temperature": cfg.llm.temperature,
            "llm_timeout_seconds": cfg.llm.timeout_seconds,
            "llm_enabled": cfg.llm.enabled,
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
            # —— 阶段超时（R8 独立计时，可在「系统设置」在线改）——
            "timeouts": cfg.timeouts.model_dump(),
            # —— 评分权重 / 时效半衰期（R11/R12，可在线调，合计必须 100）——
            "scoring": cfg.scoring.model_dump(),
            # —— 节奏随机间隔（秒）——
            "rhythm_intervals": cfg.rhythm.intervals.model_dump(),
            # —— 红线词库（R22 词库可增删；两道门禁开关仍硬锁）——
            "redline_keywords": list(cfg.ultimate_goal.redline_keywords),
            # —— 通知通道（页面可开关；webhook URL 只从 .env 读，R23 不回传）——
            "notification_channels": {
                name: {
                    "enabled": bool(ch.enabled),
                    "webhook_url_env": ch.webhook_url_env,
                    "has_webhook_url": bool(
                        ch.webhook_url_env and os.environ.get(ch.webhook_url_env)
                    ),
                    "available": True if name != "macos" else __import__(
                        "src.core.notify", fromlist=["macos_available"]
                    ).macos_available(),
                }
                for name, ch in cfg.notifications.channels.items()
            },
        },
        "overrides_file": redact_path(overrides_path(cfg)),
        "overrides": load_overrides(cfg),
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
    for win in ("ops", "draft_save"):
        if (
            f"rhythm.windows.{win}.start_hour" in normalized
            or f"rhythm.windows.{win}.end_hour" in normalized
        ):
            start = normalized.get(
                f"rhythm.windows.{win}.start_hour",
                _get_value(cfg, f"rhythm.windows.{win}.start_hour"),
            )
            end = normalized.get(
                f"rhythm.windows.{win}.end_hour",
                _get_value(cfg, f"rhythm.windows.{win}.end_hour"),
            )
            if start is not None and end is not None and start >= end:
                label = "运营时段" if win == "ops" else "存草稿时段"
                raise ConfigPatchError(f"{label} start_hour 必须小于 end_hour")

    # 平台启用开关：至少保留 1 个，否则整条生产链无法工作
    prospective = {
        p: bool(normalized.get(f"platforms.{p}.enabled", _get_value(cfg, f"platforms.{p}.enabled")))
        for p in PLATFORM_KEYS
    }
    if not any(prospective.values()):
        raise ConfigPatchError("至少必须启用 1 个平台")

    # R8：producing 超时必须小于 awaiting_publish 超时，两者独立计时但不能倒挂
    round_h = normalized.get(
        "timeouts.round_timeout_hours", _get_value(cfg, "timeouts.round_timeout_hours")
    )
    await_h = normalized.get(
        "timeouts.awaiting_publish_timeout_hours",
        _get_value(cfg, "timeouts.awaiting_publish_timeout_hours"),
    )
    if round_h is not None and await_h is not None and round_h >= await_h:
        raise ConfigPatchError("round_timeout_hours（producing）必须小于 awaiting_publish_timeout_hours")

    # R11：改权重不能破坏「每个模式合计 100%」
    for mode in SCORING_MODES:
        total = sum(
            int(normalized.get(f"scoring.{mode}.{field}", _get_value(cfg, f"scoring.{mode}.{field}")) or 0)
            for field in SCORING_WEIGHT_FIELDS
        )
        if total != 100:
            raise ConfigPatchError(
                f"{mode} 权重合计必须 = 100%，按本次修改后为 {total}%（请同时调整其它维度）"
            )

    changes: list[dict[str, Any]] = []
    overrides = load_overrides(cfg)
    for key, value in normalized.items():
        old = _jsonable(_get_value(cfg, key))
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
        _save_overrides(cfg, overrides)
        conn.commit()
        logger.info("⚙️ 配置已修改 %d 项：%s", len(changes), [c["key"] for c in changes])
    return {"changed": changes, "applied": normalized, "noop": len(changes) == 0}
