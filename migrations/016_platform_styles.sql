-- ============================================================
-- 016 — 平台文案风格（platform_styles）
--   之前四平台的标题风格 / 正文结构 / 语气 / 话题标签硬编码在
--   src/core/content_pipeline.py 的 PLATFORM_STYLES 字典里，改一次标签就要改代码。
--   现在改为库内数据，Settings 页可查看与编辑，文案生成时实时读取。
--
--   语义：
--     hashtags 是 JSON 数组，存不带 # 前缀的标签；
--     空数组 = 不加平台通用标签，正文只带选题维度标签，
--     避免把某个垂类的话题（如 #生活记录）写死到所有账号上。
-- ============================================================

CREATE TABLE IF NOT EXISTS platform_styles (
    platform    TEXT PRIMARY KEY
                CHECK (platform IN ('xhs', 'douyin', 'kuaishou', 'wechat_channels')),
    title_style TEXT NOT NULL,
    body_style  TEXT NOT NULL,
    tone        TEXT NOT NULL,
    hashtags    TEXT NOT NULL DEFAULT '[]',
    updated_at  DATETIME,
    updated_by  TEXT
);

INSERT OR IGNORE INTO platform_styles
    (platform, title_style, body_style, tone, hashtags)
VALUES
    ('xhs', 'emoji + 数字式标题，突出获得感', '分段清晰，带要点符号，结尾引导互动', '亲切、第一人称、口语化', '[]'),
    ('douyin', '口语化，直接点痛点', '短段落，节奏快，适合配字幕', '有冲击力、直接', '[]'),
    ('kuaishou', '接地气，直接给方案', '直白，不绕弯子', '朴实、真实', '[]'),
    ('wechat_channels', '正式但不生硬，偏观点式', '结构化，适合长阅读', '专业、克制', '[]');
