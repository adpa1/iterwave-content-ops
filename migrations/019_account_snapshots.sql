-- ============================================================
-- 019 — 账号级快照（粉丝总数 / 获赞总数）
--   作品级读回拿不到「新增粉丝」，涨粉类目标需要账号级数据。
--   来源：手动录入（source=manual）或账号数据读取（source=account_stats）。
--   R3：读不到的字段保持 NULL。
-- ============================================================

CREATE TABLE IF NOT EXISTS account_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    followers    INTEGER,
    likes_total  INTEGER,
    captured_at  DATETIME NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_platform_time
    ON account_snapshots (platform, captured_at);
