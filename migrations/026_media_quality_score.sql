-- 026 — 图片素材自动视觉质量初筛结果
ALTER TABLE media_assets ADD COLUMN quality_score REAL
    CHECK (quality_score IS NULL OR quality_score BETWEEN 0 AND 100);
ALTER TABLE media_assets ADD COLUMN quality_details TEXT;
