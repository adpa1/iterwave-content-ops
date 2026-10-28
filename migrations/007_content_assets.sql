-- ============================================================
-- 007 — 内容资产库（G-13：Assets 外部参考 / 个人观点 / 模板库 三个 tab）
-- 说明：只存用户真实录入的资产，不预置演示数据。
-- 外部参考 asset_type='reference'：必须带 source_url（可溯源，R22 精神）
-- 个人观点 asset_type='voice'：账号观点/立场，供后续文案参考
-- 内容模板 asset_type='template'：可复用结构说明
-- ============================================================

CREATE TABLE IF NOT EXISTS content_assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('reference','voice','template')),
    title      TEXT NOT NULL,
    body       TEXT,
    source_url TEXT,                 -- reference 必填，其余留空（NULL 不是空串）
    platform   TEXT CHECK (platform IS NULL OR platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    created_at DATETIME,
    updated_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_content_assets_type ON content_assets(asset_type, created_at);
