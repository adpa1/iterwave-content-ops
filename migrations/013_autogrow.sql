-- ============================================================
-- 013 — 自动闭环（自生长）
--   trigger_source：轮次由谁发起（manual = 用户/接口，auto = autogrow 定时任务）
--   applied_feedback：本轮实际吃进去的上一轮复盘结论（维度加减分 + 文案要点），可追溯
-- ============================================================

ALTER TABLE spiral_rounds ADD COLUMN trigger_source   TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE spiral_rounds ADD COLUMN applied_feedback TEXT;

CREATE TABLE IF NOT EXISTS autogrow_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    action      TEXT NOT NULL,          -- started / skipped / stopped / failed
    reason      TEXT NOT NULL,
    round_id    INTEGER REFERENCES spiral_rounds(id)
);
CREATE INDEX IF NOT EXISTS idx_autogrow_runs_at ON autogrow_runs(ran_at);
