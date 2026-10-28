-- ============================================================
-- 008 — ops_logs.status 补 pending_verify（R16 / R17）
-- 背景：001 建表时 CHECK 只有 success/failed/skipped/circuit_broken，
--       运营引擎写 pending_verify（主动评论/关注 崩溃灰色地带）会直接触发
--       IntegrityError，导致 R17 恢复策略无法落库。
-- 动作：SQLite 不支持修改 CHECK → 重建表（保留全部列）+ 补索引。
-- ============================================================

PRAGMA foreign_keys = OFF;

CREATE TABLE ops_logs_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER REFERENCES spiral_rounds(id),
    platform        TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    action_type     TEXT NOT NULL CHECK (action_type IN (
                        'passive_reply','active_comment','follow','browse'
                    )),
    target_url      TEXT,
    target_author   TEXT,
    content         TEXT,
    schedule_time   DATETIME,
    executed_at     DATETIME,
    status          TEXT NOT NULL CHECK (status IN (
                        'success','failed','skipped','circuit_broken','pending_verify'
                    )),
    error_msg       TEXT,
    rhythm_wait_seconds INTEGER
);

INSERT INTO ops_logs_new (
    id, round_id, platform, action_type, target_url, target_author, content,
    schedule_time, executed_at, status, error_msg, rhythm_wait_seconds
)
SELECT
    id, round_id, platform, action_type, target_url, target_author, content,
    schedule_time, executed_at, status, error_msg, rhythm_wait_seconds
FROM ops_logs;

DROP TABLE ops_logs;

ALTER TABLE ops_logs_new RENAME TO ops_logs;

CREATE INDEX IF NOT EXISTS idx_ops_logs_platform ON ops_logs(platform);
CREATE INDEX IF NOT EXISTS idx_ops_logs_action_day ON ops_logs(action_type, executed_at);

PRAGMA foreign_keys = ON;
