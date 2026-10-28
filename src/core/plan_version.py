"""
母计划版本签名（§19，R18）—— 改母计划自动使下游失效。

签名覆盖 4 个关键字段（其余字段改动不视为「换计划」）：
    topic_count_per_platform / dimension_allocation / enabled_platforms / content_format

落库位置：
- spiral_rounds.plan_signature（轮次级）
- signature_history（持久化审计，重启不丢）

用法：
    from src.core.plan_version import PlanVersionManager
    pvm = PlanVersionManager(cfg, conn)
    plan = pvm.build_plan(topic_count=5, platforms=[p.value for p in platforms],
                          dimension_allocation={"AI教育": 3, "效率工具": 2})
    pvm.record(round_id, plan)          # 写 rounds.plan_signature + signature_history
    pvm.invalidate_changed_plans()      # 找出与当前计划不一致的活跃轮次
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_SIGNATURE_FIELDS = (
    "topic_count_per_platform",
    "dimension_allocation",
    "enabled_platforms",
    "content_format",
)

_ACTIVE_STATUSES = ("created", "producing", "awaiting_publish")


class PlanVersionManager:
    """母计划签名计算、持久化与下游失效标记。"""

    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn

    # ------------------------------------------------------------------
    # 计划构造与签名
    # ------------------------------------------------------------------

    def build_plan(
        self,
        *,
        topic_count_per_platform: int,
        platforms: list[str],
        dimension_allocation: Optional[dict[str, int]] = None,
        content_format: str = "image_text",
    ) -> dict:
        return {
            "topic_count_per_platform": int(topic_count_per_platform),
            "dimension_allocation": dict(sorted((dimension_allocation or {}).items())),
            "enabled_platforms": sorted(platforms),
            "content_format": content_format,
        }

    def compute_signature(self, plan: dict) -> str:
        """对 4 个关键字段做规范化 JSON 后 sha256。"""
        payload = {k: plan.get(k) for k in _SIGNATURE_FIELDS}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def current_plan(self, topic_count_per_platform: Optional[int] = None) -> dict:
        """推导当前母计划：选题数取最近一轮实际执行的选题条数，缺省 5。"""
        if topic_count_per_platform is None:
            row = self.conn.execute(
                "SELECT topic_ids FROM spiral_rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()
            try:
                n = len(json.loads(row["topic_ids"])) if row and row["topic_ids"] else 0
            except (TypeError, ValueError):
                n = 0
            topic_count_per_platform = n or 5
        return self.build_plan(
            topic_count_per_platform=topic_count_per_platform,
            platforms=self.cfg.platforms.enabled_keys(),
        )

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    def record(self, round_id: int, plan: dict, author: str = "system") -> str:
        """写入轮次签名 + signature_history（同一签名重复记录也保留审计轨迹）。"""
        signature = self.compute_signature(plan)
        self.conn.execute(
            "UPDATE spiral_rounds SET plan_signature = ? WHERE id = ?",
            (signature, round_id),
        )
        self.conn.execute(
            "INSERT INTO signature_history (plan_signature, plan_snapshot, round_id, created_at, author) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                signature,
                json.dumps(plan, ensure_ascii=False),
                round_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                author,
            ),
        )
        self.conn.commit()
        logger.info("📌 轮次 %s 母计划签名已记录: %s", round_id, signature[:12])
        return signature

    # ------------------------------------------------------------------
    # 母计划读写（G-09 总计划 tab）
    # ------------------------------------------------------------------

    def load_plan(self) -> tuple[dict, str, Optional[str], str]:
        """读当前母计划。返回 (plan, signature, updated_at, source)。

        source = 'plan_settings'（用户在 UI 保存过）或 'derived'（从最近一轮推导）。
        """
        if self._has_plan_settings():
            row = self.conn.execute(
                "SELECT plan_snapshot, plan_signature, updated_at FROM plan_settings WHERE id = 1"
            ).fetchone()
            if row:
                try:
                    plan = json.loads(row["plan_snapshot"])
                except (TypeError, ValueError):
                    plan = self.current_plan()
                else:
                    return plan, row["plan_signature"], row["updated_at"], "plan_settings"
        plan = self.current_plan()
        return plan, self.compute_signature(plan), None, "derived"

    def save_plan(self, plan: dict, author: str = "user") -> dict:
        """保存母计划 → 写 plan_settings + signature_history + 触发下游失效（R18）。"""
        signature = self.compute_signature(plan)
        snapshot = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute(
            "INSERT INTO plan_settings (id, plan_snapshot, plan_signature, updated_at, updated_by) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET plan_snapshot = excluded.plan_snapshot, "
            "plan_signature = excluded.plan_signature, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (snapshot, signature, now, author),
        )
        self.conn.execute(
            "INSERT INTO signature_history (plan_signature, plan_snapshot, round_id, created_at, author) "
            "VALUES (?, ?, NULL, ?, ?)",
            (signature, snapshot, now, author),
        )
        self.conn.commit()

        invalidated = self.invalidate_changed_plans(plan)
        logger.info("📌 母计划已保存：签名 %s，失效轮次 %s", signature[:12], invalidated["stale_rounds"])
        return {
            "signature": signature,
            "updated_at": now,
            "plan": plan,
            "stale_rounds": invalidated["stale_rounds"],
        }

    def _has_plan_settings(self) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='plan_settings'"
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 下游失效（R18）
    # ------------------------------------------------------------------

    def invalidate_changed_plans(self, current_plan: Optional[dict] = None) -> dict:
        """把签名与当前计划不一致的活跃轮次标记为「建议重存」。

        只做标记（drafts.error_msg + rounds.transition_log 追加），不删除用户内容。
        """
        plan = current_plan or self.current_plan()
        current_sig = self.compute_signature(plan)

        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        rows = self.conn.execute(
            f"SELECT id, status, plan_signature, transition_log FROM spiral_rounds "
            f"WHERE status IN ({placeholders}) AND plan_signature IS NOT NULL",
            _ACTIVE_STATUSES,
        ).fetchall()

        stale: list[int] = []
        for row in rows:
            if row["plan_signature"] == current_sig:
                continue
            stale.append(row["id"])
            self._append_transition(row["id"], row["transition_log"],
                                    "母计划变更，本轮产物已标记为基于旧计划")
            self.conn.execute(
                "UPDATE drafts SET error_msg = ? "
                "WHERE status IN ('draft_pending','draft_saved') AND content_id IN "
                "(SELECT id FROM contents WHERE round_id = ?)",
                ("草稿内容基于旧计划，建议按新计划重存", row["id"]),
            )

        if stale:
            self.conn.commit()
            logger.warning("⚠ 母计划签名 %s 与 %d 个活跃轮次不一致: %s",
                           current_sig[:12], len(stale), stale)

        return {"current_signature": current_sig, "stale_rounds": stale}

    def _append_transition(self, round_id: int, raw: Optional[str], reason: str) -> None:
        try:
            log = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            log = []
        log.append({
            "from": None,
            "to": None,
            "at": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
        })
        self.conn.execute(
            "UPDATE spiral_rounds SET transition_log = ? WHERE id = ?",
            (json.dumps(log, ensure_ascii=False), round_id),
        )
