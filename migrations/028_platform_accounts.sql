-- Registered platform identities are explicit user-maintained records.
-- Counts represent accounts registered here, not inferred login sessions or platform snapshots.
CREATE TABLE IF NOT EXISTS platform_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    account_handle TEXT NOT NULL,
    normalized_handle TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    notes TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, normalized_handle)
);
CREATE INDEX IF NOT EXISTS idx_platform_accounts_platform_status
    ON platform_accounts(platform, status);
