-- ============================================================
-- 018 — 目标层级：总目标（north_star）+ 平台子目标（platform）
--   goal_level      north_star = 账号矩阵总目标；platform = 某平台阶段目标
--   parent_goal_id  平台目标挂到哪个总目标下（hybrid 模式汇总用；可为空）
--   weight          汇总到总目标时的权重（为空按 1 计）
--   contribution_note  这个平台目标如何促进总目标（如「小红书种草 → 视频号转化」）
-- 旧数据全部视为平台目标，行为不变。
-- ============================================================

ALTER TABLE goals ADD COLUMN goal_level TEXT DEFAULT 'platform';
ALTER TABLE goals ADD COLUMN parent_goal_id TEXT;
ALTER TABLE goals ADD COLUMN weight REAL;
ALTER TABLE goals ADD COLUMN contribution_note TEXT;
