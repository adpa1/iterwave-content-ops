-- ============================================================
-- 003 — 通知事件类型扩展（P1-3 Notifier）
-- 001 的 notifications.type 只覆盖 7 个场景，但熔断恢复 / 发布确认 /
-- 配额耗尽同样是必须留痕的事件。SQLite 无法直接改 CHECK，按官方建议重建表。
-- 保留原有 7 个 type（R14），新增 3 个。
-- ============================================================

CREATE TABLE notifications_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL CHECK (level IN ('info','warning','error','critical')),
    type       TEXT NOT NULL CHECK (type IN (
                    'draft_saved','draft_failed','circuit_broken','login_expired',
                    'goal_completed','data_fetch_failed','round_completed',
                    'circuit_recovered','publish_confirmed','quota_exhausted'
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
