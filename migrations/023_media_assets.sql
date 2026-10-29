-- 023 — 按账号组隔离的可复用图片素材库
-- content_assets（007）是参考资料/观点/模板，不改其语义；图片文件通过 media_assets 管理。
CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL CHECK (mime_type IN ('image/jpeg','image/png','image/webp','image/gif')),
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    sha256 TEXT NOT NULL,
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    alt_text TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    source_type TEXT NOT NULL DEFAULT 'upload' CHECK (source_type IN ('upload','generated','imported')),
    source_ref TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_media_assets_status_created
    ON media_assets(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_assets_sha256
    ON media_assets(sha256);

CREATE TABLE IF NOT EXISTS media_asset_usages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,
    content_id INTEGER REFERENCES contents(id) ON DELETE SET NULL,
    platform TEXT CHECK (platform IS NULL OR platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    usage_role TEXT NOT NULL DEFAULT 'illustration'
        CHECK (usage_role IN ('cover','illustration','background')),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_media_asset_usages_asset
    ON media_asset_usages(media_asset_id);
CREATE INDEX IF NOT EXISTS idx_media_asset_usages_content
    ON media_asset_usages(content_id);
