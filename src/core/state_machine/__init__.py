"""
两层状态机 — 轮次 9 态 + 单篇 8 态（R7/R12）。

所有状态变更必须修改 DB（drafts / spiral_rounds 表），
**只 toast 不更新状态是 bug**（AGENTS.md §8.3）。

R10 analyzing 三条退出路径：
- 有 ≥1 篇 published 且全部 fetch_status=success/partial → completed
- 有 ≥1 篇 published 且有 cancelled/failed/expired → partial_completed
- 全部 published 都 fetch_status=failed → failed
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# =====================================================================
# 轮次状态机（9 态）
# =====================================================================

ROUND_STATUSES = (
    "created", "producing", "awaiting_publish",
    "collecting_metrics", "analyzing",
    "completed", "partial_completed", "cancelled", "failed",
)

# 合法迁移表：from → set[to]
ROUND_TRANSITIONS: dict[str, set[str]] = {
    "created":           {"producing", "cancelled"},
    "producing":         {"awaiting_publish", "failed", "cancelled"},
    "awaiting_publish":  {"collecting_metrics", "cancelled"},
    "collecting_metrics": {"analyzing", "cancelled"},
    "analyzing":         {"completed", "partial_completed", "failed"},
    "completed":         set(),         # 终态
    "partial_completed": set(),         # 终态
    "failed":            set(),         # 终态
    "cancelled":         set(),         # 终态
}


class RoundStateMachine:
    """轮次状态机（spiral_rounds.status）。"""

    def __init__(self, db_conn):
        self.db = db_conn

    # ------------------------------------------------------------------
    def can_transition(self, round_id: int, target: str) -> tuple[bool, str]:
        """检查从当前状态迁移到 target 是否合法。同状态视为幂等。"""
        row = self.db.execute(
            "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
        ).fetchone()
        if not row:
            return False, f"轮次 {round_id} 不存在"
        current = row[0]
        if current == target:
            return True, "同状态幂等"
        if target not in ROUND_TRANSITIONS.get(current, set()):
            return False, f"非法迁移: {current} → {target}"
        return True, ""

    def transition(self, round_id: int, target: str, reason: str = "") -> bool:
        """执行状态变更，记录 transition_log。同状态幂等不报错。"""
        row = self.db.execute(
            "SELECT status, transition_log FROM spiral_rounds WHERE id = ?", (round_id,)
        ).fetchone()
        if not row:
            logger.error("轮次 %s 不存在", round_id)
            return False
        current = row[0]
        if current == target:
            logger.debug("轮次 %s 已是 %s（幂等）", round_id, target)
            return True
        ok, msg = self.can_transition(round_id, target)
        if not ok:
            logger.error("轮次状态变更失败: round=%s %s", round_id, msg)
            return False

        try:
            log = json.loads(row[1]) if row[1] else []
        except (json.JSONDecodeError, TypeError):
            log = []

        log.append({
            "from": current,
            "to": target,
            "at": datetime.now().isoformat(),
            "reason": reason,
        })

        # R8：阶段起点独立落库，供 round_timeout_sweeper 分阶段判定超时
        now_str = datetime.now().isoformat(timespec="seconds")
        fields: dict = {
            "status": target,
            "transition_log": json.dumps(log, ensure_ascii=False),
        }
        if target == "producing":
            fields["producing_started_at"] = now_str
        elif target == "collecting_metrics":
            fields["collecting_metrics_started_at"] = now_str

        sql = "UPDATE spiral_rounds SET " + ", ".join(f"{k} = ?" for k in fields) + " WHERE id = ?"
        self.db.execute(sql, (*fields.values(), round_id))
        self.db.commit()
        logger.info("🔄 轮次 %s: %s → %s (%s)", round_id, current, target, reason)
        return True

    # ------------------------------------------------------------------
    # 轮次流转触发（R9/R10）
    # ------------------------------------------------------------------

    def on_first_publish_confirmed(self, round_id: int) -> None:
        """首篇 user_published → 进入 collecting_metrics。

        D6 守卫：若轮次仍处 producing（如异步确认 / 干跑 save_drafts=false），
        先补齐 awaiting_publish 再进 collecting_metrics，避免 transition 静默失败卡死。
        轮询发布确认(D2)与手动确认(D6)共用此入口，均受益。
        """
        current = self.db.execute(
            "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
        ).fetchone()
        if not current:
            return
        if current[0] == "producing":
            self.transition(round_id, "awaiting_publish",
                            "发布确认前补齐：producing→awaiting_publish")
        self.transition(round_id, "collecting_metrics", "首篇发布确认")

    def decide_analyzing_exit(self, round_id: int) -> str:
        """R10 analyzing 三条退出路径 — 返回目标状态。

        - 有 ≥1 篇 published 且全部 fetch_status=success/partial 且无掉队篇目 → completed
        - 有 ≥1 篇 published 且有 cancelled/failed/expired 篇目 → partial_completed
        - 全部 published 都 fetch_status=failed → failed
        """
        row = self.db.execute(
            "SELECT d.status, m.fetch_status FROM drafts d "
            "LEFT JOIN metrics m ON d.id = m.draft_id AND m.attempt = (SELECT MAX(attempt) FROM metrics m2 WHERE m2.draft_id = d.id) "
            "JOIN contents c ON d.content_id = c.id "
            "WHERE c.round_id = ?",
            (round_id,),
        ).fetchall()

        statuses = [r[0] for r in row]
        # 已发布的 draft
        published_rows = [r for r in row if r[0] == "published"]
        if not published_rows:
            # 没有已发布篇目 → 整轮 failed
            return "failed"

        fetch_statuses = [r[1] or "pending" for r in published_rows]

        # R10 第三路：全部已发布篇目采集失败 → failed
        if all(fs == "failed" for fs in fetch_statuses):
            return "failed"

        # R10 第二路：存在掉队篇目（cancelled / failed / expired）→ partial_completed
        # 与模块头 R10 定义一致：存草稿阶段就 failed 的篇目同样是本轮没交付的内容，
        # 计入掉队；否则「1 篇成功 + 1 篇平台存草稿失败」会被报成 completed，夸大完成度（R3）。
        # 仍处在 draft_pending / draft_saved 的篇目交给超时收口，不在这里判定。
        if any(s in ("cancelled", "failed", "expired") for s in statuses):
            return "partial_completed"

        # R10 第一路：全部采集成功/部分成功 → completed
        if all(fs in ("success", "partial") for fs in fetch_statuses):
            return "completed"

        # 采集仍未落定（pending）→ 按部分完成处理，交给超时收口
        return "partial_completed"


# =====================================================================
# 单篇草稿状态机（8 态）
# =====================================================================

DRAFT_STATUSES = (
    "draft_pending", "draft_saved", "user_published",
    "data_fetching", "published", "failed", "cancelled", "expired",
)

DRAFT_TRANSITIONS: dict[str, set[str]] = {
    "draft_pending":    {"draft_saved", "failed", "cancelled"},
    "draft_saved":      {"user_published", "failed", "cancelled", "expired"},
    "user_published":   {"data_fetching", "failed"},
    "data_fetching":    {"published", "failed"},
    "published":        set(),           # 终态
    "failed":           set(),           # 终态
    "cancelled":        set(),           # 终态
    "expired":          set(),           # 终态
}


class DraftStateMachine:
    """单篇草稿状态机（drafts.status）。"""

    def __init__(self, db_conn):
        self.db = db_conn

    def transition(
        self, draft_id: str, target: str, reason: str = "",
        extra_updates: Optional[dict] = None,
    ) -> bool:
        """执行单篇状态变更。同状态幂等（R16）—— 不报错、不写日志。"""
        row = self.db.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if not row:
            logger.error("草稿 %s 不存在", draft_id)
            return False
        current = row[0]

        if current == target:
            logger.debug("草稿 %s 已是 %s（幂等）", draft_id, target)
            return True

        if target not in DRAFT_TRANSITIONS.get(current, set()):
            logger.error("非法迁移: draft %s %s → %s", draft_id, current, target)
            return False

        update_fields = {"status": target}
        if target == "draft_saved":
            update_fields["saved_at"] = datetime.now().isoformat()
        if target == "user_published":
            update_fields["user_confirmed_at"] = datetime.now().isoformat()
        if extra_updates:
            update_fields.update(extra_updates)

        sets = ", ".join(f"{k} = ?" for k in update_fields)
        values = list(update_fields.values()) + [draft_id]
        self.db.execute(f"UPDATE drafts SET {sets} WHERE id = ?", values)
        self.db.commit()
        logger.info("🔄 草稿 %s: %s → %s (%s)", draft_id, current, target, reason)
        return True

    # ------------------------------------------------------------------
    # R8 独立超时判定
    # ------------------------------------------------------------------

    def collect_metrics_phase_timeout(
        self, round_id: int, now: Optional[datetime] = None, timeout_hours: int = 72,
    ) -> bool:
        """R8：collecting_metrics 阶段是否超时（按轮次 collecting_metrics_started_at 计时）。

        只做判定、不改任何草稿状态 —— 阶段超时与单篇 publish_confirm 超时互不影响。
        """
        from datetime import timedelta
        row = self.db.execute(
            "SELECT status, collecting_metrics_started_at FROM spiral_rounds WHERE id = ?",
            (round_id,),
        ).fetchone()
        if not row or row[0] != "collecting_metrics" or not row[1]:
            return False
        try:
            started = datetime.fromisoformat(row[1])
        except ValueError:
            return False
        return (now or datetime.now()) - started > timedelta(hours=timeout_hours)

    def check_expired_drafts(
        self, now: Optional[datetime] = None,
        timeout_hours: int = 72, grace_hours: int = 48,
    ) -> list[str]:
        """publish_confirm_timeout + grace 后仍未确认发布的单篇 → expired。

        R8：超时参数由调用方传 config（timeouts.publish_confirm_timeout_hours /
        grace_hours），与轮次阶段超时互相独立；R9：按各自 saved_at 独立计时。
        """
        from datetime import timedelta
        now = now or datetime.now()
        cursor = self.db.execute(
            "SELECT id, saved_at, created_at FROM drafts WHERE status = 'draft_saved'"
        )
        expired_ids: list[str] = []
        for draft_id, saved_at_str, created_at_str in cursor.fetchall():
            # 老版本/迁移数据可能没有 saved_at；以创建时间兜底，避免待发布草稿永远无法过期。
            candidates = [value for value in (saved_at_str, created_at_str) if value]
            saved_at = None
            for candidate in candidates:
                try:
                    saved_at = datetime.fromisoformat(str(candidate))
                    break
                except ValueError:
                    continue
            if saved_at is None:
                logger.warning("草稿 %s 缺少有效 saved_at / created_at，无法自动判过期", draft_id)
                continue
            total_wait = timedelta(hours=timeout_hours + grace_hours)
            if now - saved_at > total_wait:
                self.transition(draft_id, "expired", reason="publish_confirm_timeout + grace 到期")
                expired_ids.append(draft_id)
        return expired_ids
