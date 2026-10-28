-- 015 · 通知规则（notify_rules）
-- 用途：按「场景 × 通道」覆盖 config.notifications 默认值（Notifier._rule_override 读取）。
-- 语义：某场景一旦存在规则，就只按规则里 enabled=1 的通道发送，级别取第一条启用规则；
--       该场景没有规则 → 完全按 config 默认（不改历史行为）。
CREATE TABLE IF NOT EXISTS notify_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario   TEXT NOT NULL,
    channel    TEXT NOT NULL CHECK (channel IN ('macos', 'wecom', 'feishu', 'console')),
    level      TEXT NOT NULL DEFAULT 'warning'
               CHECK (level IN ('info', 'warning', 'error', 'critical')),
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    condition  TEXT,
    notes      TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_notify_rules_scenario_channel
    ON notify_rules (scenario, channel);
