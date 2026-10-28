#!/usr/bin/env python3
"""Phase 1 启动验证 — 确认 config + DB schema 全部生效."""
from __future__ import annotations

import sys
import logging
from pathlib import Path

# 让 src/ 可 import
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from src.core.config import load_config  # noqa: E402
from src.core.db import Database  # noqa: E402


def main() -> int:
    print("=" * 60)
    print("Phase 1 启动验证")
    print("=" * 60)

    # 1. 加载配置
    print("\n[1/4] 加载 config.yaml ...")
    cfg = load_config()
    print(f"  ✅ 启用平台: {cfg.platforms.enabled_keys()}")
    print(f"  ✅ 选题库: {cfg.topic_kb.path}")
    print(f"  ✅ 冷启动权重和: {sum([cfg.scoring.cold_start.position_match, cfg.scoring.cold_start.history_performance, cfg.scoring.cold_start.timeliness, cfg.scoring.cold_start.gap_weight, cfg.scoring.cold_start.freshness])}")
    print(f"  ✅ 稳定运行权重和: {sum([cfg.scoring.stable_operation.position_match, cfg.scoring.stable_operation.history_performance, cfg.scoring.stable_operation.timeliness, cfg.scoring.stable_operation.gap_weight, cfg.scoring.stable_operation.freshness])}")
    print(f"  ✅ producing 超时: {cfg.timeouts.round_timeout_hours}h, awaiting_publish 超时: {cfg.timeouts.awaiting_publish_timeout_hours}h (R8 独立)")

    # 2. 初始化数据库
    print("\n[2/4] 初始化 SQLite + schema 迁移 ...")
    db = Database(cfg)
    db.initialize()
    print(f"  ✅ DB 文件: {db.db_path}")

    # 3. 验证 schema 完整性
    print("\n[3/4] 验证 schema 完整性 ...")
    report = db.verify_schema()

    if report["missing_tables"]:
        print(f"  ❌ 缺失表: {report['missing_tables']}")
        return 1
    print(f"  ✅ 全部 {len(report['existing_tables'])} 张表已创建")

    # R14: drafts 表必须有 expired 状态
    if "drafts_has_expired" in report:
        if report["drafts_has_expired"]:
            print("  ✅ drafts.status CHECK 包含 'expired'（R14 红线通过）")
        else:
            print("  ❌ drafts.status CHECK 缺失 'expired'（R14 红线）")
            return 1

    # R7: spiral_rounds 表必须有全部 9 态
    if "rounds_has_all_9_statuses" in report:
        if report["rounds_has_all_9_statuses"]:
            print("  ✅ spiral_rounds.status CHECK 包含全部 9 态（R7 通过）")
        else:
            print("  ❌ spiral_rounds.status CHECK 不完整（R7 红线）")
            return 1

    # 4. 插入一条测试数据并回读（验证 NULL vs 0 R3）
    print("\n[4/4] 写入 + 回读测试（验证 NULL vs 0 R3）...")
    conn = db.connect()

    # goals 表：current_value 应为 NULL，不能默认 0
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR REPLACE INTO goals (id, goal_type, goal_text, target_metric, target_value, target_platform, metric_scope, data_source) "
        "VALUES ('test-goal-001', 'stage', '测试目标', 'followers_gained', 500, 'xhs', 'delta', 'account_stats')"
    )
    conn.commit()

    row = conn.execute(
        "SELECT current_value FROM goals WHERE id='test-goal-001'"
    ).fetchone()
    current_value = row[0]
    if current_value is None:
        print(f"  ✅ goals.current_value = NULL（未采集到就是 NULL，R3 通过）")
    else:
        print(f"  ❌ goals.current_value = {current_value}（应为 NULL，R3 红线！）")
        conn.close()
        return 1

    # metrics 表：likes/views 应为 NULL
    conn.execute(
        "INSERT OR REPLACE INTO task_runs (task_type, biz_id, platform, status, attempt, max_attempts) "
        "VALUES ('kb_scan', 'test', 'xhs', 'success', 0, 3)"
    )
    conn.commit()

    # 查看 metrics 表 DDL 确认没有 DEFAULT 0
    cur = conn.execute("SELECT sql FROM sqlite_master WHERE name='metrics'")
    ddl = cur.fetchone()[0] or ""
    if "INTEGER" in ddl and "DEFAULT 0" not in ddl.split("INTEGER")[0].split("(")[-1].split(")")[0] if False else True:
        # 简单检查：metrics 表整体不含 DEFAULT 0
        pass
    # 更直接：看 likes/views 列定义里有没有 DEFAULT 0
    for col_name in ("likes", "views", "collects", "comments", "shares", "new_followers"):
        # 找该列定义
        import re
        m = re.search(rf"{col_name}\s+INTEGER([^,\)]*)", ddl)
        if m and "DEFAULT 0" in m.group(1):
            print(f"  ❌ metrics.{col_name} 有 DEFAULT 0（R3 红线！）")
            conn.close()
            return 1
    print("  ✅ metrics 各指标列无 DEFAULT 0（R3 通过）")

    # 清理测试数据
    conn.execute("DELETE FROM task_runs WHERE biz_id='test'")
    conn.execute("DELETE FROM goals WHERE id='test-goal-001'")
    conn.execute("DELETE FROM schema_version")  # 保留表，清空版本让下次迁移重新跑
    conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print("🎉 Phase 1 验证全部通过！")
    print("=" * 60)
    print("\n下一步：Phase 2 — 平台适配器接口 + 存草稿器")
    return 0


if __name__ == "__main__":
    sys.exit(main())
