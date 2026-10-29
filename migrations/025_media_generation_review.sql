-- 025 — AI 配图生成审计、月度配额与人工审核
ALTER TABLE media_assets ADD COLUMN review_status TEXT NOT NULL DEFAULT 'approved'
    CHECK (review_status IN ('pending_review','approved','rejected'));

CREATE TABLE IF NOT EXISTS media_generation_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
    monthly_quota INTEGER NOT NULL DEFAULT 20 CHECK (monthly_quota BETWEEN 0 AND 1000),
    prompt_prefix TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'gpt-image-1',
    size TEXT NOT NULL DEFAULT '1024x1024'
        CHECK (size IN ('1024x1024','1024x1536','1536x1024')),
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS media_generation_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt TEXT NOT NULL,
    final_prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
    output_asset_id INTEGER REFERENCES media_assets(id) ON DELETE SET NULL,
    error_message TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_media_generation_jobs_created
    ON media_generation_jobs(created_at DESC);
