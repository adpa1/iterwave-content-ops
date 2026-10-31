-- 一个账号组内每个平台只能登记一个账号（包含 inactive 状态）。
-- 先由 Database._preflight_platform_account_singleton 检查历史重复记录。
CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_accounts_one_per_platform
ON platform_accounts(platform);
