"""
配置加载模块 — 读取 config.yaml + .env，输出类型化 Pydantic 配置对象。

所有运行时模块从这里读配置，严禁各自硬编码或直接 open yaml 文件。
时间统一本地时区（R5），严禁 datetime.utcnow()。

用法：
    from src.core.config import Config, load_config
    cfg = load_config()
    print(cfg.platforms.xhs.enabled)
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# =====================================================================
# 枚举值常量（与 DB CHECK 约束保持一致，R7）
# =====================================================================

PLATFORM_KEYS = ("xhs", "douyin", "kuaishou", "wechat_channels")
PLATFORM_DB_NAMES = {
    "xhs": "xhs",
    "douyin": "douyin",
    "kuaishou": "kuaishou",
    "wechat_channels": "wechat_channels",
}

ROUND_STATUSES = (
    "created", "producing", "awaiting_publish",
    "collecting_metrics", "analyzing",
    "completed", "partial_completed", "cancelled", "failed",
)

DRAFT_STATUSES = (
    "draft_pending", "draft_saved", "user_published",
    "data_fetching", "published", "failed", "cancelled", "expired",
)

FETCH_STATUSES = ("pending", "success", "partial", "failed", "parse_error")


# =====================================================================
# Pydantic 配置模型（按 config.yaml 结构镜像）
# =====================================================================

class PathsConfig(BaseModel):
    data_dir: str = "data"
    db_file: str = "data/easel.db"
    backup_dir: str = "backups"
    output_dir: str = "data/outputs"
    screenshot_dir: str = "data/screenshots"
    log_dir: str = "data/logs"


class TopicKbConfig(BaseModel):
    path: str
    type: str = "auto"
    table_name: str = "topics"
    field_mapping: dict[str, str] = Field(default_factory=dict)


class PlatformConfig(BaseModel):
    enabled: bool = True
    name: str
    base_url: str
    account_key: str


class PlatformsConfig(BaseModel):
    xhs: PlatformConfig
    douyin: PlatformConfig
    kuaishou: PlatformConfig
    wechat_channels: PlatformConfig

    def enabled_keys(self) -> list[str]:
        """返回当前启用的平台 key 列表。"""
        return [k for k, v in self.model_dump().items()
                if isinstance(v, dict) and v.get("enabled")]


class ScoringModeConfig(BaseModel):
    position_match: int = 0
    history_performance: int = 0
    timeliness: int = 0
    gap_weight: int = 0
    freshness: int = 0

    @field_validator("*")
    @classmethod
    def _check_weight_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(f"权重必须在 0-100 之间，收到 {v}")
        return v


class ScoringConfig(BaseModel):
    cold_start: ScoringModeConfig
    sample_accumulating: ScoringModeConfig
    stable_operation: ScoringModeConfig
    timeliness_half_life_days: dict[str, int] = Field(
        default_factory=lambda: {"news": 7, "evergreen": 365}
    )
    active_dimension_min_uses: int = 2
    active_dimension_window_days: int = 90
    shrinkage_m: int = 5

    @model_validator(mode="after")
    def _check_weight_sum(self) -> "ScoringConfig":
        """三模式权重和必须 = 100%。"""
        for mode_name, mode in (
            ("cold_start", self.cold_start),
            ("sample_accumulating", self.sample_accumulating),
            ("stable_operation", self.stable_operation),
        ):
            total = (
                mode.position_match + mode.history_performance
                + mode.timeliness + mode.gap_weight + mode.freshness
            )
            if total != 100:
                raise ValueError(
                    f"评分模式 {mode_name} 权重和 = {total}，必须 = 100"
                )
        return self


class UltimateGoalConfig(BaseModel):
    persona_keywords: list[str] = Field(default_factory=list)
    redline_keywords: list[str] = Field(default_factory=list)
    forbidden_directions: list[str] = Field(default_factory=list)


class TimeoutsConfig(BaseModel):
    round_timeout_hours: int = 48
    awaiting_publish_timeout_hours: int = 120
    publish_confirm_timeout_hours: int = 72
    publish_confirm_grace_hours: int = 48
    collecting_metrics_timeout_hours: int = 72
    metrics_initial_delay_hours: int = 24
    metrics_retry_policy: str = "3次/间隔30min"
    goal_max_rounds: int = 30
    round_min_interval_hours: int = 4

    @model_validator(mode="after")
    def _check_independent_timeouts(self) -> "TimeoutsConfig":
        """R8 红线：producing 超时 < awaiting_publish，避免重叠覆盖。"""
        if self.round_timeout_hours >= self.awaiting_publish_timeout_hours:
            raise ValueError(
                "round_timeout_hours（producing）必须 < awaiting_publish_timeout_hours，"
                "否则会出现 producing 阶段超时后 awaiting_publish 阶段还没开始就被整轮终止"
            )
        return self


class CircuitBreakerConfig(BaseModel):
    signals: list[str] = Field(
        default_factory=lambda: [
            "captcha", "rate_limit", "login_expired", "action_blocked", "device_verify",
        ]
    )
    cooldown_minutes: int = 30
    recovery: dict[str, bool] = Field(
        default_factory=lambda: {
            "require_cooldown_expired": True,
            "require_test_login": True,
            "require_lightweight_action": True,
        }
    )


class RhythmWindowsConfig(BaseModel):
    start_hour: int = 8
    end_hour: int = 23


class RhythmIntervalsConfig(BaseModel):
    passive_reply_base_seconds: int = 60
    passive_reply_jitter_seconds: int = 30
    active_comment_base_seconds: int = 300
    active_comment_jitter_seconds: int = 120
    follow_base_seconds: int = 600
    follow_jitter_seconds: int = 300


class PlatformQuotaConfig(BaseModel):
    passive_replies: int = 0
    active_comments: int = 0
    follows: int = 0


class RhythmConfig(BaseModel):
    daily_quotas: dict[str, PlatformQuotaConfig]
    windows: dict[str, RhythmWindowsConfig] = Field(
        default_factory=lambda: {
            "ops": RhythmWindowsConfig(start_hour=8, end_hour=23),
            "draft_save": RhythmWindowsConfig(start_hour=9, end_hour=22),
        }
    )
    intervals: RhythmIntervalsConfig


class ActiveCommentTarget(BaseModel):
    """主动评论目标（用户维护）。content 必须过终极目标检查才允许发送。"""

    url: str
    content: str


class OpsTargetsConfig(BaseModel):
    """单平台的主动运营目标清单（空 = 该平台不派发主动动作）。"""

    active_comments: list[ActiveCommentTarget] = Field(default_factory=list)
    follows: list[str] = Field(default_factory=list)


class NotificationChannelConfig(BaseModel):
    enabled: bool = False
    webhook_url_env: Optional[str] = None


class NotificationEventConfig(BaseModel):
    name: str
    level: str
    enabled: bool = True


class NotificationsConfig(BaseModel):
    channels: dict[str, NotificationChannelConfig] = Field(default_factory=dict)
    events: list[NotificationEventConfig] = Field(default_factory=list)


class HotTopicsConfig(BaseModel):
    enabled: bool = True
    refresh_interval_minutes: int = 30
    sources: list[dict] = Field(default_factory=list)
    filters: dict[str, object] = Field(default_factory=dict)


class DataRetentionConfig(BaseModel):
    raw_response_days: int = 90
    screenshot_days: int = 30
    ops_logs_days: int = 365
    ai_calls_detail_days: int = 180


# =====================================================================
# 顶层 Config
# =====================================================================

class LlmConfig(BaseModel):
    """文案生成用 LLM（Agent Plan / 方舟）。无 key 时自动降级模板。"""
    enabled: bool = False
    provider: str = "ark"
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    model: str = "doubao-seed-1-6-250615"
    api_key_env: str = "ARK_API_KEY"          # R23：只从 .env 读，不落库不落日志
    temperature: float = 0.7
    timeout_seconds: int = 60


class Config(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    topic_kb: TopicKbConfig
    platforms: PlatformsConfig
    scoring: ScoringConfig
    ultimate_goal: UltimateGoalConfig
    timeouts: TimeoutsConfig
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    rhythm: RhythmConfig
    # 主动运营目标清单（G-03）：平台 → 评论目标 / 关注目标；为空则该平台不派发
    ops_targets: dict[str, OpsTargetsConfig] = Field(default_factory=dict)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    hot_topics: HotTopicsConfig = Field(default_factory=HotTopicsConfig)
    data_retention: DataRetentionConfig = Field(default_factory=DataRetentionConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)

    def resolve_path(self, relative: str) -> Path:
        """把 config 中的相对路径解析为相对于项目根的绝对路径。"""
        return PROJECT_ROOT / relative


# =====================================================================
# 加载入口
# =====================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # iterwave-content-ops/
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / "config" / ".env"


def load_config(
    config_path: Optional[Path | str] = None,
    env_path: Optional[Path | str] = None,
) -> Config:
    """加载配置文件 + .env 密钥覆盖，返回类型化 Config 对象。"""
    from dotenv import load_dotenv

    # 1. 加载 .env（密钥，R23 不进 git）
    env_p = Path(env_path) if env_path else DEFAULT_ENV_PATH
    if env_p.exists():
        load_dotenv(env_p)
        logger.info("已加载 .env: %s", env_p)

    # 2. 加载 config.yaml
    cfg_p = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not cfg_p.exists():
        raise FileNotFoundError(
            f"找不到配置文件: {cfg_p}，请在 config/config.yaml 创建"
        )

    with open(cfg_p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # 3. 校验 + 转 Pydantic
    cfg = Config.model_validate(raw)

    # 4. 确保必要目录存在
    for dir_name in (cfg.paths.data_dir, cfg.paths.backup_dir,
                     cfg.paths.output_dir, cfg.paths.screenshot_dir,
                     cfg.paths.log_dir):
        dir_path = cfg.resolve_path(dir_name)
        dir_path.mkdir(parents=True, exist_ok=True)

    logger.info(
        "配置加载完成 · 启用平台: %s · 选题库: %s",
        cfg.platforms.enabled_keys(), cfg.topic_kb.path,
    )
    return cfg


# =====================================================================
# 便捷访问：全局单例（首次调用时加载）
# =====================================================================

_CONFIG: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置单例（线程不安全，启动时调用一次后只读）。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
