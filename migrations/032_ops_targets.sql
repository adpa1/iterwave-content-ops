-- 032 — User-maintained active operation targets.
CREATE TABLE IF NOT EXISTS ops_targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    action_type TEXT NOT NULL CHECK (action_type IN ('active_comment','follow')),
    target      TEXT NOT NULL,
    content     TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_targets_dispatch
    ON ops_targets(platform, action_type, sort_order, id);
