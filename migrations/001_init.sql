-- =====================================================================
-- Redbook Easel — 初始数据库 schema (v001)
-- 基于设计方案 v1.3.1 + RULES.md v1.4
-- 所有 CHECK 约束、NOT NULL 约束、索引严格按设计方案定义
-- 时间统一本地时区（应用层控制），DB 层用 DATETIME 存 ISO 字符串
-- 指标字段全部可空（R3/R15：NULL ≠ 0，禁止 DEFAULT 0）
-- =====================================================================

PRAGMA foreign_keys = ON;

-- =====================================================================
-- 版本追踪
-- =====================================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    description TEXT NOT NULL
);

-- =====================================================================
-- 目标追踪（§4）
-- v1.3 补目标口径（R21）：未知进度必须为 NULL，禁止 DEFAULT 0
-- =====================================================================
CREATE TABLE IF NOT EXISTS goals (
    id               TEXT PRIMARY KEY,
    goal_type        TEXT NOT NULL DEFAULT 'stage'
                       CHECK (goal_type IN ('stage','milestone')),
    goal_text        TEXT NOT NULL,
    target_metric    TEXT NOT NULL,
    target_value     REAL NOT NULL,
    target_platform  TEXT,
    -- ===== 目标口径字段 =====
    metric_scope     TEXT NOT NULL DEFAULT 'delta'
                       CHECK (metric_scope IN ('delta','total')),
    baseline_value   REAL,           -- ⚠️ 无 DEFAULT，未采集到就是 NULL
    baseline_at      DATETIME,
    metric_period_start DATETIME,
    metric_period_end   DATETIME,
    data_source      TEXT,           -- account_stats / manual / platform_api
    current_value    REAL,           -- ⚠️ 禁止 DEFAULT 0，R21
    progress_updated_at DATETIME,
    status           TEXT DEFAULT 'active'
                       CHECK (status IN ('active','completed','failed','cancelled','paused')),
    rejected_by_ultimate_goal INTEGER DEFAULT 0,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at     DATETIME,
    persona_file     TEXT
);

-- =====================================================================
-- 螺旋轮次（§12，9 态状态机）
-- R7 所有状态值必须与应用层状态机表完全同步
-- =====================================================================
CREATE TABLE IF NOT EXISTS spiral_rounds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id      TEXT REFERENCES goals(id),
    round_number INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'created'
                   CHECK (status IN (
                       'created','producing','awaiting_publish',
                       'collecting_metrics','analyzing',
                       'completed','partial_completed','cancelled','failed'
                   )),
    topic_ids    TEXT NOT NULL,          -- JSON array
    content_variants TEXT NOT NULL,      -- JSON array
    draft_ids    TEXT,                   -- JSON array
    plan_signature TEXT,                 -- §19 母计划签名
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    transition_log JSON,                 -- [{from, to, at, reason}]
    user_published_at DATETIME,          -- 首篇 user_published 时间
    data_fetched_at   DATETIME,          -- 首篇数据采集完成
    awaiting_publish_ended_at DATETIME,  -- awaiting_publish 超时判定
    collecting_metrics_started_at DATETIME,  -- collecting_metrics 起点
    decision     JSON,                   -- 复盘 SpiralDecision
    gap_suggestion JSON,                 -- gap_analyzer 输出
    duration_hours REAL
);

-- =====================================================================
-- 选题库索引（§15 选题评估用）
-- =====================================================================
CREATE TABLE IF NOT EXISTS topic_index (
    id                    TEXT PRIMARY KEY,
    dimension             TEXT,
    source                TEXT,
    title                 TEXT,
    date                  TEXT,
    first_seen_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    total_uses            INTEGER DEFAULT 0,
    avg_interaction_rate  REAL
);

-- =====================================================================
-- 内容产物
-- =====================================================================
CREATE TABLE IF NOT EXISTS contents (
    id         TEXT PRIMARY KEY,
    round_id   INTEGER REFERENCES spiral_rounds(id),
    topic_id   TEXT REFERENCES topic_index(id),
    platform   TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    tags       TEXT,                     -- JSON array
    image_paths TEXT,                    -- JSON array
    word_count INTEGER,
    card_style TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(round_id, topic_id, platform)
);

-- =====================================================================
-- 草稿记录（§12，8 态状态机）
-- R7 所有状态值必须与应用层状态机表完全同步
-- expired 不能漏（R14 红线）
-- =====================================================================
CREATE TABLE IF NOT EXISTS drafts (
    id                    TEXT PRIMARY KEY,
    content_id            TEXT REFERENCES contents(id),
    platform              TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    draft_id              TEXT,          -- 平台返回的草稿 ID
    status                TEXT DEFAULT 'draft_pending'
                           CHECK (status IN (
                               'draft_pending','draft_saved','user_published',
                               'data_fetching','published','failed','cancelled','expired'
                           )),
    saved_at              DATETIME,      -- Playwright 存草稿成功
    user_confirmed_at     DATETIME,      -- 用户点"✅ 我已发布"
    scheduled_publish_at  DATETIME,      -- §24.3 排期发布时间
    error_msg             TEXT,
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(content_id, platform)
);

-- =====================================================================
-- 数据指标（§14）
-- R3/R15：所有数值列全部可空，禁止 DEFAULT 0
-- fetch_status 五态：pending/success/partial/failed/parse_error
-- =====================================================================
CREATE TABLE IF NOT EXISTS metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id     TEXT REFERENCES drafts(id),
    content_id   TEXT REFERENCES contents(id),
    platform     TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    fetch_status TEXT NOT NULL DEFAULT 'pending'
                   CHECK (fetch_status IN ('pending','success','partial','failed','parse_error')),
    fetched_at   DATETIME,
    attempt      INTEGER DEFAULT 0,
    -- 原始响应留存
    raw_response TEXT,                   -- JSON
    -- ⚠️ 全部无 DEFAULT，NULL=未知，0=真实零值
    likes        INTEGER,
    collects     INTEGER,
    comments     INTEGER,
    shares       INTEGER,
    views        INTEGER,
    new_followers INTEGER,
    -- 归一化指标（views=NULL/0 时全部为 NULL，不做除法）
    like_rate         REAL,
    collect_rate      REAL,
    interaction_rate  REAL,
    follower_efficiency REAL,
    error_msg    TEXT,
    UNIQUE(draft_id, fetched_at)
);

-- =====================================================================
-- 运营动作日志（§6 幂等覆盖所有外部操作）
-- =====================================================================
CREATE TABLE IF NOT EXISTS ops_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER REFERENCES spiral_rounds(id),
    platform        TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    action_type     TEXT NOT NULL CHECK (action_type IN (
                        'passive_reply','active_comment','follow','browse'
                    )),
    target_url      TEXT,
    target_author   TEXT,
    content         TEXT,
    schedule_time   DATETIME,
    executed_at     DATETIME,
    status          TEXT NOT NULL CHECK (status IN (
                        'success','failed','skipped','circuit_broken'
                    )),
    error_msg       TEXT,
    rhythm_wait_seconds INTEGER
);

-- =====================================================================
-- 熔断事件（§5 停止所有运营 + 3 步恢复验证 R6）
-- =====================================================================
CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    reason          TEXT NOT NULL CHECK (reason IN (
                        'captcha','rate_limit','login_expired','action_blocked','device_verify'
                    )),
    detected_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    cooldown_until  DATETIME,
    resolved_at     DATETIME,
    resolved_by     TEXT CHECK (resolved_by IN ('auto','user'))
);

-- =====================================================================
-- 配置变更日志（审计用）
-- =====================================================================
CREATE TABLE IF NOT EXISTS config_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- =====================================================================
-- 单平台纵深分析结果（§1 v1.1 新增）
-- =====================================================================
CREATE TABLE IF NOT EXISTS platform_insights (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id         INTEGER REFERENCES spiral_rounds(id),
    platform         TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    analyzed_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    days_analyzed    INTEGER,
    trend            TEXT,               -- JSON
    best_hours       TEXT,               -- JSON array
    best_title_style TEXT,               -- JSON
    best_card_style  TEXT,               -- JSON
    top_dimensions   TEXT,               -- JSON array
    UNIQUE(round_id, platform)
);

-- =====================================================================
-- 消息通知记录
-- =====================================================================
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL CHECK (level IN ('info','warning','error','critical')),
    type       TEXT NOT NULL CHECK (type IN (
                    'draft_saved','draft_failed','circuit_broken','login_expired',
                    'goal_completed','data_fetch_failed','round_completed'
                )),
    platform   TEXT CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    title      TEXT NOT NULL,
    body       TEXT,
    channel    TEXT CHECK (channel IN ('macos','wecom','feishu')),
    sent_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(type, platform, sent_at)      -- 同事件同 cron window 只发一次（R16 幂等）
);

-- =====================================================================
-- 任务持久化（§13，APScheduler 执行轨迹）
-- 与 drafts/ops_logs 边界：这里只记调度层，业务状态记在别处
-- =====================================================================
CREATE TABLE IF NOT EXISTS task_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type   TEXT NOT NULL CHECK (task_type IN (
                    'publish_polling','data_fetch','daily_ops','comment_polling',
                    'cb_recovery','kb_scan','draft_save','round_pipeline','data_retention_cleanup'
                )),
    biz_id      TEXT,                     -- draft_id / round_id / platform
    platform    TEXT CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    status      TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','running','interrupted','success','failed','skipped','pending_verify')),
    attempt     INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    started_at  DATETIME,
    finished_at DATETIME,
    next_run_at DATETIME,
    error_msg   TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- =====================================================================
-- AI 调用审计（§16.3，v1.3 统一字段，禁止两套并存）
-- R23 数据保留：明细 180 天后删除，聚合统计永久保留
-- =====================================================================
CREATE TABLE IF NOT EXISTS ai_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    step          TEXT NOT NULL,          -- topic-evaluator / copywriter / spiral-engine ...
    module        TEXT,                   -- 兼容旧字段名，同 step
    model         TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    algo_version  TEXT,
    input_digest  TEXT,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    latency_ms    INTEGER NOT NULL,
    duration_ms   INTEGER,                -- 兼容旧字段名，同 latency_ms
    output_valid  INTEGER,                -- 1=校验通过, 0=校验失败
    success       INTEGER NOT NULL,       -- 1=成功, 0=失败
    attempt       INTEGER DEFAULT 1,
    called_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- =====================================================================
-- user_override 人工覆盖记录（§16.3）
-- =====================================================================
CREATE TABLE IF NOT EXISTS user_override (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id         INTEGER REFERENCES spiral_rounds(id),
    ai_call_id       INTEGER REFERENCES ai_calls(id),
    override_field   TEXT,
    original_value   TEXT,
    override_value   TEXT,
    reason           TEXT NOT NULL,
    overridden_by    TEXT NOT NULL,
    overridden_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- =====================================================================
-- signature_history 母计划签名持久化（§19）
-- =====================================================================
CREATE TABLE IF NOT EXISTS signature_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_signature  TEXT NOT NULL,
    plan_snapshot   TEXT NOT NULL,         -- JSON（TEXT 存 SQLite JSON）
    round_id        INTEGER REFERENCES spiral_rounds(id),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    author          TEXT
);

-- =====================================================================
-- 热点发现（§20，R22 必须有来源证据）
-- =====================================================================
CREATE TABLE IF NOT EXISTS hot_topics (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,          -- 60s.viki.moe 等
    raw_url         TEXT,
    discovered_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    evidence_status TEXT NOT NULL CHECK (evidence_status IN ('collected','demo','failed')),
    title           TEXT,
    heat_score      REAL,                   -- 热度数字
    heat_unit       TEXT,                   -- 讨论量 / 指数
    time_sensitive  INTEGER DEFAULT 0,
    dimension       TEXT
);

-- =====================================================================
-- 索引
-- =====================================================================
CREATE INDEX IF NOT EXISTS idx_drafts_status      ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_platform    ON drafts(platform);
CREATE INDEX IF NOT EXISTS idx_metrics_content    ON metrics(content_id);
CREATE INDEX IF NOT EXISTS idx_metrics_fetch      ON metrics(fetch_status);
CREATE INDEX IF NOT EXISTS idx_ops_logs_platform  ON ops_logs(platform);
CREATE INDEX IF NOT EXISTS idx_task_runs_status   ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_task_runs_biz       ON task_runs(biz_id, task_type);
CREATE INDEX IF NOT EXISTS idx_topic_dimension    ON topic_index(dimension);
CREATE INDEX IF NOT EXISTS idx_rounds_goal         ON spiral_rounds(goal_id);
CREATE INDEX IF NOT EXISTS idx_metrics_platform   ON metrics(platform);
CREATE INDEX IF NOT EXISTS idx_drafts_saved_at    ON drafts(saved_at);
CREATE INDEX IF NOT EXISTS idx_circuit_breaker_platform ON circuit_breaker_events(platform, resolved_at);
CREATE INDEX IF NOT EXISTS idx_notifications_sent ON notifications(sent_at);
