-- ============================================================
-- 017 — drafts.platform_note_url
--   用户手动发布后补填的平台笔记链接。
--   comment_polling 之前用内部 draft id（draft-xxxx）拼
--   https://www.xiaohongshu.com/explore/draft-xxxx，必然 404，
--   导致被动回复永远拉不到评论（G-02②③ 的真实阻断点）。
--   有该链接才轮询；没有就跳过，不再请求无效地址。
-- ============================================================

ALTER TABLE drafts ADD COLUMN platform_note_url TEXT;
