"""
已生产内容 / 轮次删除。

规则（不丢数据闭环）：
- 已发布链路上的内容（user_published / data_fetching / published）不能删：它们有指标，
  删了会让复盘、目标进度、评分历史失真。需要隐藏请改用驳回/取消。
- draft_saved：平台创作者中心里还留着这份草稿，系统不会去平台删（R1 只存草稿、不做平台写删除），
  返回 platform_drafts_left 提醒用户自己去平台清理。
- 删除内容时一并删本地卡片图片文件（只删项目 output 目录下的文件）。
- 轮次只有在没有任何不可删内容时才能整轮删除；进行中的 producing 轮次不能删。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LOCKED_DRAFT_STATUSES = ("user_published", "data_fetching", "published")
BUSY_ROUND_STATUSES = ("producing", "collecting_metrics", "analyzing")


class DeleteBlocked(ValueError):
    pass


def _remove_files(cfg, raw) -> int:
    try:
        paths = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return 0
    root = cfg.resolve_path(cfg.paths.output_dir).resolve()
    n = 0
    for p in paths or []:
        try:
            f = Path(p).resolve()
            if root in f.parents and f.is_file():
                f.unlink()
                n += 1
        except OSError:
            logger.warning("卡片文件删除失败：%s", p)
    return n


def delete_contents(cfg, conn, ids: list[str]) -> dict:
    ids = [i for i in dict.fromkeys(str(x).strip() for x in ids) if i]
    if not ids:
        raise ValueError("ids 不能为空")
    deleted, blocked, not_found = [], [], []
    platform_left = 0
    files = 0
    for cid in ids:
        row = conn.execute("SELECT id, image_paths FROM contents WHERE id = ?", (cid,)).fetchone()
        if row is None:
            not_found.append(cid)
            continue
        drafts = conn.execute("SELECT id, status FROM drafts WHERE content_id = ?", (cid,)).fetchall()
        if any(d[1] in LOCKED_DRAFT_STATUSES for d in drafts):
            blocked.append({"id": cid, "reason": "已发布（有数据），不能删除；可在内容资产里归档查看"})
            continue
        platform_left += sum(1 for d in drafts if d[1] == "draft_saved")
        draft_ids = [d[0] for d in drafts]
        if draft_ids:
            marks = ",".join("?" * len(draft_ids))
            conn.execute(f"DELETE FROM metrics WHERE draft_id IN ({marks})", draft_ids)
        conn.execute("DELETE FROM metrics WHERE content_id = ?", (cid,))
        conn.execute("DELETE FROM drafts WHERE content_id = ?", (cid,))
        conn.execute("DELETE FROM contents WHERE id = ?", (cid,))
        files += _remove_files(cfg, row[1])
        deleted.append(cid)
    conn.commit()
    return {"deleted": deleted, "blocked": blocked, "not_found": not_found,
            "platform_drafts_left": platform_left, "files_removed": files}


def delete_round(cfg, conn, round_id: int) -> dict:
    row = conn.execute("SELECT id, round_number, status FROM spiral_rounds WHERE id = ?", (round_id,)).fetchone()
    if row is None:
        raise LookupError(f"轮次 {round_id} 不存在")
    if row[2] in BUSY_ROUND_STATUSES:
        raise DeleteBlocked(f"第 {row[1]} 轮正在 {row[2]}，不能删除；等它结束或先取消")
    locked = conn.execute(
        "SELECT COUNT(*) FROM drafts d JOIN contents c ON d.content_id = c.id "
        f"WHERE c.round_id = ? AND d.status IN ({','.join('?' * len(LOCKED_DRAFT_STATUSES))})",
        (round_id, *LOCKED_DRAFT_STATUSES),
    ).fetchone()[0]
    if locked:
        raise DeleteBlocked(f"第 {row[1]} 轮有 {locked} 篇已发布内容（有数据），不能整轮删除")
    cids = [r[0] for r in conn.execute("SELECT id FROM contents WHERE round_id = ?", (round_id,))]
    res = delete_contents(cfg, conn, cids) if cids else {"deleted": [], "platform_drafts_left": 0, "files_removed": 0}
    for table in ("ops_logs", "platform_insights", "user_override", "autogrow_runs"):
        try:
            conn.execute(f"UPDATE {table} SET round_id = NULL WHERE round_id = ?", (round_id,))
        except Exception:  # noqa: BLE001 — 旧库缺表 / 列
            pass
    conn.execute("UPDATE signature_history SET round_id = NULL WHERE round_id = ?", (round_id,))
    try:
        conn.execute("DELETE FROM task_runs WHERE task_type = 'round_pipeline' AND biz_id = ?", (str(round_id),))
    except Exception:  # noqa: BLE001
        pass
    conn.execute("DELETE FROM spiral_rounds WHERE id = ?", (round_id,))
    conn.commit()
    return {"round_id": round_id, "round_number": row[1], "contents_deleted": len(res["deleted"]),
            "platform_drafts_left": res["platform_drafts_left"], "files_removed": res["files_removed"]}
