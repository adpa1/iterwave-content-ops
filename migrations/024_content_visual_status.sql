-- 024 — 明确区分模板降级卡片与正式视觉素材
-- 历史记录默认为 degraded：旧 image_paths 可能只是本地渐变模板，不能推断为正式配图。
ALTER TABLE contents ADD COLUMN visual_status TEXT NOT NULL DEFAULT 'degraded'
    CHECK (visual_status IN ('degraded','media_asset','generated'));
ALTER TABLE contents ADD COLUMN visual_reason TEXT;

UPDATE contents
SET visual_reason = '历史内容未记录正式视觉素材来源，需人工确认'
WHERE visual_status = 'degraded' AND visual_reason IS NULL;
