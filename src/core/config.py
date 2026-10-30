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
    # 没有维度列时归到这个默认维度（不猜测垂类）
    default_dimension: str = "综合"


class RenderConfig(BaseModel):
    """图片卡片渲染（可移植：字体路径可在配置 / .env 指定）。"""
    font_path: Optional[str] = None
    card_tag_fallback: str = "今日分享"


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
    cold_start_min_published: int = Field(10, ge=1, le=1000)
    stable_dimension_min_samples: int = Field(5, ge=1, le=100)
    history_normalization_scale: int = Field(2000, ge=1, le=10000)
    freshness_window_days: int = Field(30, ge=1, le=365)
    dimension_gap_ratio: float = Field(0.5, ge=0.0, le=1.0)
    dimension_gap_score: float = Field(90.0, ge=0.0, le=100.0)
    dimension_normal_score: float = Field(40.0, ge=0.0, le=100.0)
    no_persona_match_score: float = Field(60.0, ge=0.0, le=100.0)
    stable_exploration_percent: int = Field(15, ge=0, le=50)
    timeliness_unknown_score: float = Field(50.0, ge=0.0, le=100.0)
    history_cold_start_score: float = Field(0.0, ge=0.0, le=100.0)
    freshness_recent_use_penalty: float = Field(20.0, ge=0.0, le=100.0)
    freshness_min_score: float = Field(10.0, ge=0.0, le=100.0)
    freshness_unused_score: float = Field(100.0, ge=0.0, le=100.0)

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
        if self.stable_dimension_min_samples < self.active_dimension_min_uses:
            raise ValueError("stable_dimension_min_samples 必须大于等于 active_dimension_min_uses")
        return self


class PlatformPersonaConfig(BaseModel):
    """单平台账号画像（不同平台登录的可能是不同账号、不同定位）。

    inherit_global=True：在全局画像基础上追加本平台关键词 / 禁区；
    inherit_global=False：本平台完全使用自己的画像（红线词库始终叠加全局，安全底线不可绕过）。
    """
    inherit_global: bool = True
    account_name: str = ""
    positioning: str = ""
    audience: str = ""
    # 本平台在矩阵里的角色：traffic 引流 / conversion 转化 / brand 品牌 / community 社群 / test 试验
    role: str = ""
    persona_keywords: list[str] = Field(default_factory=list)
    forbidden_directions: list[str] = Field(default_factory=list)
    extra_redlines: list[str] = Field(default_factory=list)


class PlatformLinkConfig(BaseModel):
    """平台之间的协同关系（一个平台为另一个平台导流 / 复用素材 / 互推）。"""
    source: str
    target: str
    relation: str = "traffic"   # traffic 导流 / reuse 素材复用 / cross 互推
    note: str = ""


class UltimateGoalConfig(BaseModel):
    persona_keywords: list[str] = Field(default_factory=list)
    redline_keywords: list[str] = Field(default_factory=list)
    forbidden_directions: list[str] = Field(default_factory=list)
    # 一句话账号定位（全局）
    positioning: str = ""
    # unified：全平台共用一个画像与一个总目标
    # per_platform：每个平台独立画像、独立目标，互不汇总
    # hybrid：一个总目标 + 各平台画像/子目标，平台之间可配置协同关系
    goal_mode: str = "unified"
    platform_personas: dict[str, PlatformPersonaConfig] = Field(default_factory=dict)
    platform_links: list[PlatformLinkConfig] = Field(default_factory=list)


class TimeoutsConfig(BaseModel):
    round_timeout_hours: int = 48
    awaiting_publish_timeout_hours: int = 120
    publish_confirm_timeout_hours: int = 72
    publish_confirm_grace_hours: int = 48
    collecting_metrics_timeout_hours: int = 72
    metrics_initial_delay_hours: int = 24
    metrics_retry_attempts: int = Field(3, ge=1, le=20)
    metrics_retry_interval_minutes: int = Field(30, ge=1, le=1440)
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
    # 三步恢复验证是安全流程，固定在代码中，不提供可关闭的配置项。
    cooldown_minutes: int = Field(30, ge=1, le=1440)
    recovery_failure_extension_minutes: int = Field(60, ge=1, le=1440)


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


class HotTopicDedupConfig(BaseModel):
    """热点去重规则（采集入库时执行）。"""
    # 规则 1：同一平台标题归一化（去空白 / 标点 / emoji、全角转半角、英文小写）后相同 → 同一条，只更新热度与 last_seen_at
    # 规则 2：跨平台标题归一化后相同，或字符二元组相似度 ≥ 阈值 → 标记 duplicate_of 主条目（热度最高者），雷达只展示一张卡
    cross_platform_similarity: float = Field(0.8, ge=0.5, le=1.0)
    # 规则 3：只在这个时间窗内找跨平台重复（避免把一个月前的同名旧闻并进来）
    cross_platform_window_hours: int = Field(48, ge=1, le=720)


class HotTopicsConfig(BaseModel):
    enabled: bool = True
    # 已废弃：采集频率改由 scheduler.jobs.hot_topics_refresh 配置（写了也不再生效，启动时告警）
    refresh_interval_minutes: Optional[int] = None
    sources: list[dict] = Field(default_factory=list)
    filters: dict[str, object] = Field(default_factory=dict)
    # 滚动：每次采集只保留当前榜单（本轮没再上榜的旧热点立即删除，不展示也不参与研判）
    # retention_days / max_rows 仅做兜底清理（采集失败占位、异常残留）
    retention_days: int = Field(7, ge=1, le=365)
    max_rows: int = Field(5000, ge=100, le=1_000_000)
    dedup: HotTopicDedupConfig = Field(default_factory=HotTopicDedupConfig)
    max_items_per_source: int = Field(30, ge=1, le=500)
    fetch_timeout_seconds: int = Field(15, ge=1, le=120)
    max_concurrent_fetches: int = Field(8, ge=1, le=32)
    trend_burst_multiplier: float = Field(1.5, ge=1.01, le=10.0)
    trend_cooled_ratio: float = Field(0.6, ge=0.05, le=0.95)
    trend_sustained_ratio: float = Field(0.9, ge=0.05, le=1.0)
    trend_previous_drop_ratio: float = Field(0.9, ge=0.05, le=1.0)
    trend_min_seen_count: int = Field(3, ge=2, le=20)

    @model_validator(mode="after")
    def _check_trend_policy(self) -> "HotTopicsConfig":
        if self.trend_cooled_ratio >= self.trend_sustained_ratio:
            raise ValueError("trend_cooled_ratio 必须小于 trend_sustained_ratio")
        return self


# =====================================================================
# 定时任务（APScheduler）—— 每个任务的开关与触发方式全部来自配置，代码里不写死
# =====================================================================

# 任务 id → 说明（未在 config.yaml 中出现的任务使用 DEFAULT_JOB_SCHEDULES 的默认值）
JOB_DESCRIPTIONS: dict[str, str] = {
    "publish_polling": "发布轮询（确认用户已在平台发布）",
    "daily_ops": "每日运营（主动评论 / 关注）",
    "comment_polling": "评论轮询（被动回复）",
    "cb_recovery": "熔断恢复检查",
    "kb_scan": "选题库增量扫描",
    "kb_sync": "远程选题库每日同步",
    "data_retention_cleanup": "过期数据清理",
    "round_timeout_sweeper": "轮次阶段超时扫描",
    "round_review": "轮次复盘收口",
    "hot_topics_refresh": "热点采集 + 标签匹配",
    "autogrow_next_round": "自动闭环：满足条件时自动开下一轮并生成草稿",
}


class JobScheduleConfig(BaseModel):
    """单个定时任务：interval_minutes 与 cron 二选一。cron 为 5 段 crontab（本地时区，R5）。"""
    enabled: bool = True
    interval_minutes: Optional[int] = Field(None, ge=1, le=10080)
    cron: Optional[str] = None

    @model_validator(mode="after")
    def _one_trigger(self) -> "JobScheduleConfig":
        if (self.interval_minutes is None) == (self.cron is None):
            raise ValueError("interval_minutes 与 cron 必须且只能配置一个")
        if self.cron is not None:
            validate_cron(self.cron)
        return self

    def describe(self) -> str:
        if self.cron:
            return f"cron {self.cron}"
        return f"每 {self.interval_minutes} 分钟"


def validate_cron(expr: str) -> str:
    """校验 5 段 crontab；非法直接抛 ValueError（不做静默兜底）。"""
    from apscheduler.triggers.cron import CronTrigger

    expr = (expr or "").strip()
    if len(expr.split()) != 5:
        raise ValueError(f"cron 必须是 5 段 crontab（分 时 日 月 周），收到：{expr!r}")
    try:
        CronTrigger.from_crontab(expr)
    except ValueError as e:
        raise ValueError(f"cron 表达式不合法：{expr!r}（{e}）") from e
    return expr


DEFAULT_JOB_SCHEDULES: dict[str, dict] = {
    "publish_polling": {"interval_minutes": 30},
    "daily_ops": {"cron": "0 8 * * *"},
    "comment_polling": {"interval_minutes": 30},
    "cb_recovery": {"interval_minutes": 5},
    "kb_scan": {"cron": "0 6 * * *"},
    "kb_sync": {"cron": "0 7 * * *"},
    "data_retention_cleanup": {"cron": "10 6 * * *"},
    "round_timeout_sweeper": {"interval_minutes": 60},
    "round_review": {"interval_minutes": 10},
    "hot_topics_refresh": {"interval_minutes": 120},
    "autogrow_next_round": {"interval_minutes": 60},
}


class SchedulerConfig(BaseModel):
    jobs: dict[str, JobScheduleConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_defaults(self) -> "SchedulerConfig":
        unknown = set(self.jobs) - set(DEFAULT_JOB_SCHEDULES)
        if unknown:
            raise ValueError(f"未知定时任务：{sorted(unknown)}，可选 {sorted(DEFAULT_JOB_SCHEDULES)}")
        for job_id, default in DEFAULT_JOB_SCHEDULES.items():
            if job_id not in self.jobs:
                self.jobs[job_id] = JobScheduleConfig(**default)
        return self

    def job(self, job_id: str) -> JobScheduleConfig:
        return self.jobs[job_id]


class AutogrowConfig(BaseModel):
    """自动闭环（自生长）：复盘结论 → 下一轮选题/文案，并按条件自动开下一轮。

    安全边界：自动开轮只生成草稿，发布仍需用户在平台确认（R9 人工闸门不变）；
    目标达成、连续失败、每日上限、冷却期、全平台熔断任一命中都不开轮。
    """
    enabled: bool = True                     # 总开关：关掉后不自动开轮（复盘结论仍可喂给评分）
    apply_feedback: bool = True              # 复盘结论是否进入选题评分与文案提示词
    topic_count: int = Field(5, ge=1, le=50)  # 自动轮每平台选题数
    cooldown_hours: int = Field(24, ge=0, le=720)   # 上一轮收口后至少间隔多久再开
    max_rounds_per_day: int = Field(1, ge=0, le=24)  # 每天最多自动开几轮（0 = 暂停）
    max_consecutive_failures: int = Field(2, ge=1, le=20)  # 连续失败 N 轮后自动暂停
    stop_when_goals_achieved: bool = True    # 所有目标达成 → 停止自动开轮
    feedback_max_bias: float = Field(15.0, ge=0, le=50)  # 单维度评分加/减分上限（总分 0-100 制）
    feedback_lookback_rounds: int = Field(3, ge=1, le=20)  # 取最近 N 轮复盘结论
    recency_decay: float = Field(0.5, ge=0.0, le=1.0)
    full_confidence_samples: int = Field(5, ge=1, le=100)
    coverage_gap_bias_ratio: float = Field(0.6, ge=0.0, le=1.0)
    validation_gap_bias_ratio: float = Field(0.3, ge=0.0, le=1.0)
    min_dimension_bias: float = Field(0.5, ge=0.0, le=10.0)
    max_reference_titles_per_platform: int = Field(2, ge=0, le=10)
    max_copy_hints_per_platform: int = Field(6, ge=1, le=20)
    max_copy_hint_chars: int = Field(120, ge=20, le=500)


class VisionRankingConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout_seconds: int = Field(25, ge=1, le=120)
    candidate_limit: int = Field(8, ge=1, le=32)
    max_image_bytes: int = Field(4 * 1024 * 1024, ge=1024, le=4 * 1024 * 1024)
    max_body_chars: int = Field(1800, ge=100, le=10000)
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(600, ge=1, le=4000)

    @field_validator("base_url")
    @classmethod
    def _safe_vision_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("vision_ranking.base_url 必须是无认证信息的 HTTP(S) URL")
        return value.strip().rstrip("/")


class MediaGenerationConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: int = Field(150, ge=1, le=300)
    connect_timeout_seconds: int = Field(15, ge=1, le=60)
    max_response_bytes: int = Field(20 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    max_dimension: int = Field(20000, ge=256, le=20000)
    max_pixels: int = Field(40_000_000, ge=1_000_000, le=40_000_000)

    @field_validator("base_url")
    @classmethod
    def _safe_generation_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("media_generation.base_url 必须是无认证信息的 HTTP(S) URL")
        return value.strip().rstrip("/")


class MediaQualityConfig(BaseModel):
    resolution_weight: float = Field(25.0, ge=0.0, le=100.0)
    composition_weight: float = Field(20.0, ge=0.0, le=100.0)
    contrast_weight: float = Field(20.0, ge=0.0, le=100.0)
    brightness_weight: float = Field(15.0, ge=0.0, le=100.0)
    sharpness_weight: float = Field(20.0, ge=0.0, le=100.0)
    color_diversity_weight: float = Field(20.0, ge=0.0, le=100.0)
    resolution_reference_pixels: int = Field(1_500_000, ge=100_000, le=20_000_000)
    portrait_ratio_min: float = Field(0.55, ge=0.1, le=1.0)
    portrait_ratio_max: float = Field(0.85, ge=0.2, le=2.0)
    square_ratio_max: float = Field(1.1, ge=0.5, le=3.0)
    landscape_ratio_max: float = Field(1.7, ge=1.0, le=5.0)
    brightness_min: float = Field(45.0, ge=0.0, le=255.0)
    brightness_max: float = Field(210.0, ge=0.0, le=255.0)
    brightness_penalty_scale: float = Field(8.5, ge=0.1, le=64.0)
    contrast_reference: float = Field(45.0, ge=1.0, le=128.0)
    sharpness_reference: float = Field(500.0, ge=1.0, le=10000.0)
    sharpness_warning_threshold: float = Field(35.0, ge=0.0, le=1000.0)
    low_contrast_threshold: float = Field(18.0, ge=0.0, le=128.0)
    low_resolution_pixels: int = Field(500_000, ge=10_000, le=20_000_000)
    low_color_count: int = Field(3, ge=1, le=16)
    color_count_reference: int = Field(16, ge=1, le=256)
    composition_near_portrait_multiplier: float = Field(0.85, ge=0.0, le=1.0)
    composition_square_multiplier: float = Field(0.6, ge=0.0, le=1.0)
    composition_landscape_multiplier: float = Field(0.25, ge=0.0, le=1.0)
    sharpness_fallback_contrast_reference: float = Field(60.0, ge=1.0, le=255.0)
    sampling_size: int = Field(512, ge=128, le=2048)
    color_quantization_colors: int = Field(16, ge=2, le=256)

    @model_validator(mode="after")
    def _check_quality_ranges(self) -> "MediaQualityConfig":
        if self.portrait_ratio_min >= self.portrait_ratio_max:
            raise ValueError("portrait_ratio_min 必须小于 portrait_ratio_max")
        if self.portrait_ratio_max >= self.square_ratio_max:
            raise ValueError("portrait_ratio_max 必须小于 square_ratio_max")
        if self.square_ratio_max >= self.landscape_ratio_max:
            raise ValueError("square_ratio_max 必须小于 landscape_ratio_max")
        if self.brightness_min >= self.brightness_max:
            raise ValueError("brightness_min 必须小于 brightness_max")
        return self


class TenantSwitchConfig(BaseModel):
    request_drain_timeout_seconds: int = Field(30, ge=1, le=120)
    scheduler_drain_timeout_seconds: int = Field(30, ge=1, le=120)
    round_drain_timeout_seconds: int = Field(120, ge=1, le=600)
    background_jobs_timeout_seconds: int = Field(120, ge=1, le=600)
    login_process_timeout_seconds: int = Field(10, ge=1, le=60)


class TenantBackupConfig(BaseModel):
    max_bytes: int = Field(2 * 1024 * 1024 * 1024, ge=1_048_576, le=2 * 1024 * 1024 * 1024)
    retention_count: int = Field(10, ge=1, le=100)


class HotTopicMatcherConfig(BaseModel):
    match_threshold: float = Field(60.0, ge=0.0, le=100.0)
    llm_temperature: float = Field(0.1, ge=0.0, le=2.0)
    keyword_score: float = Field(60.0, ge=0.0, le=100.0)
    batch_size: int = Field(20, ge=1, le=100)
    max_per_run: int = Field(200, ge=1, le=2000)


class CopyGenerationConfig(BaseModel):
    """文案生成的可调输出约束；提示词结构与安全规则仍由代码版本化。"""
    topic_summary_max_chars: int = Field(300, ge=50, le=2000)
    title_max_chars: int = Field(20, ge=5, le=40)
    body_min_chars: int = Field(150, ge=0, le=5000)
    body_max_chars: int = Field(400, ge=50, le=10000)
    tag_min_count: int = Field(3, ge=0, le=20)
    tag_max_count: int = Field(5, ge=1, le=30)
    persona_keyword_limit: int = Field(8, ge=1, le=50)
    forbidden_direction_limit: int = Field(5, ge=1, le=50)
    strategy_hint_max_count: int = Field(6, ge=0, le=20)
    strategy_hint_max_chars: int = Field(120, ge=20, le=500)
    max_guard_retries: int = Field(2, ge=0, le=10)

    @model_validator(mode="after")
    def _check_copy_constraints(self) -> "CopyGenerationConfig":
        if self.body_min_chars > self.body_max_chars:
            raise ValueError("body_min_chars 必须小于等于 body_max_chars")
        if self.tag_min_count > self.tag_max_count:
            raise ValueError("tag_min_count 必须小于等于 tag_max_count")
        return self


class InsightsConfig(BaseModel):
    """平台纵深分析的窗口、缓存与结果条数。"""
    window_days: int = Field(90, ge=1, le=3650)
    cache_max_age_minutes: int = Field(60, ge=1, le=10080)
    top_dimensions_limit: int = Field(5, ge=1, le=20)
    top_hours_limit: int = Field(3, ge=1, le=24)


class PlanSuggestionConfig(BaseModel):
    """目标拆分建议策略；不替用户自动承诺产能。"""
    default_topic_count: int = Field(5, ge=1, le=50)
    default_period_days: int = Field(28, ge=1, le=3650)
    minimum_sample_count: int = Field(3, ge=1, le=100)
    max_topics_per_platform: int = Field(50, ge=1, le=200)
    max_rounds: int = Field(60, ge=1, le=500)
    default_interval_days: int = Field(7, ge=1, le=365)


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
    # ⚠️ 通道选择（重要，关乎计费与可用性，R23 安全）：
    #   - /api/v3      → 按量付费通道
    #   - /api/plan/v3 → 专属 Agent Plan 通道（Agent Plan 只支持套餐内模型）
    # 「系统设置 → 主模型与备用」可在线切换，最终以 config.yaml / config_overrides.json 为准。
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    model: str = "doubao-seed-2.1-lite"
    fallback_model: str = "doubao-seed-2.0-mini"   # 主模型失败时的兜底模型；空串 = 不启用
    api_key_env: str = "ARK_API_KEY"          # R23：只从 .env 读，不落库不落日志
    temperature: float = 0.7
    timeout_seconds: int = 60


class Config(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    topic_kb: TopicKbConfig
    render: RenderConfig = Field(default_factory=RenderConfig)
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
    vision_ranking: VisionRankingConfig = Field(default_factory=VisionRankingConfig)
    media_generation: MediaGenerationConfig = Field(default_factory=MediaGenerationConfig)
    media_quality: MediaQualityConfig = Field(default_factory=MediaQualityConfig)
    tenant_switch: TenantSwitchConfig = Field(default_factory=TenantSwitchConfig)
    tenant_backup: TenantBackupConfig = Field(default_factory=TenantBackupConfig)
    hot_topic_matcher: HotTopicMatcherConfig = Field(default_factory=HotTopicMatcherConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    autogrow: AutogrowConfig = Field(default_factory=AutogrowConfig)
    copy_generation: CopyGenerationConfig = Field(default_factory=CopyGenerationConfig)
    insights: InsightsConfig = Field(default_factory=InsightsConfig)
    plan_suggestion: PlanSuggestionConfig = Field(default_factory=PlanSuggestionConfig)

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

    # 图片字体：配置 / .env 指定的路径写入环境变量，渲染模块统一读取
    if cfg.render.font_path:
        os.environ.setdefault("ITERWAVE_FONT_PATH", cfg.render.font_path)
    os.environ.setdefault("CARD_TAG_FALLBACK", cfg.render.card_tag_fallback)

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
