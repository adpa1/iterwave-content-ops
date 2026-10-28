-- ============================================================
-- 004 — R8 轮次阶段超时（producing_started_at + round_timeout 通知类型）
-- producing 超时 48h 需要独立起点列；awaiting_publish 用 transition_log，
-- collecting_metrics 用已有的 collecting_metrics_started_at。
-- 同时扩展 notifications.type 支持超时通知（SQLite 需重建表改 CHECK）。
-- ============================================================

ALTER TABLE spiral_rounds ADD COLUMN producing_started_at DATETIME;

CREATE TABLE notifications_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL CHECK (level IN ('info','warning','error','critical')),
    type       TEXT NOT NULL CHECK (type IN (
                    'draft_saved','draft_failed','circuit_broken','login_expired',
                    'goal_completed','data_fetch_failed','round_completed',
                    'circuit_recovered','publish_confirmed','quota_exhausted',
                    'round_timeout'
                )),
    platform   TEXT CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    title      TEXT NOT NULL,
    body       TEXT,
    channel    TEXT CHECK (channel IN ('macos','wecom','feishu')),
    sent_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(type, platform, sent_at)
);

INSERT INTO notifications_new (id, level, type, platform, title, body, channel, sent_at)
SELECT id, level, type, platform, title, body, channel, sent_at FROM notifications;

DROP TABLE notifications;

ALTER TABLE notifications_new RENAME TO notifications;

CREATE INDEX IF NOT EXISTS idx_notifications_sent ON notifications(sent_at);
