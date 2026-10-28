"""
轮次执行器 —— 把 §4 主流程真正串起来（P0-1）。

主流程（R2 红线，G → G2 → H 必须串行）：
    created → producing
      ① 读选题库（KbReader）
      ② G  选题评分（ScoringEngine 三模式）
      ③ 四平台文案生成（generate_copy_stub → P1 换 LLM）
      ④ G2 终极目标检查（UltimateGoalChecker，FAIL 丢弃 / ERROR 不放行）
      ⑤ H  图片卡片制作
      ⑥ 写 contents + drafts（draft_pending）
      ⑦ 逐平台存草稿（DraftSaver，R1：只点"存草稿"，永不点"发布"）
    producing → awaiting_publish（全部 draft_saved/failed）

约束：
- R1  只存草稿，不点发布
- R2  G2 检查失败必须丢弃；检查器异常（ERROR）不能默认通过
- R3  NULL ≠ 0（本模块不写任何指标数字）
- R5  本地时区 datetime.now()
- R16 存草稿幂等：content_id + platform 已有 draft_saved 则跳过
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from .config import PROJECT_ROOT
from .kb_reader import KbReader
from .content_pipeline import generate_copy_content, make_card, _llm_available
from .plan_version import PlanVersionManager
from .guard.ultimate_goal_checker import CheckOutcome, CheckResult
from ..adapters.base import Platform, ContentToPublish
from ..adapters.base import DraftStatus, CircuitBreakerReason

logger = logging.getLogger(__name__)

# 平台未启用 / 熔断时不给该平台产出内容
_SKIP_REASON_CIRCUIT = "平台熔断中，跳过本轮产出"

# R2：G2 FAIL/ERROR → 丢弃重生成，最多重试 2 次（即最多 3 次生成）
MAX_GUARD_RETRIES = 2


class RoundExecutor:
    """轮次执行器：选题 → 文案 → 检查 → 卡片 → 存草稿。"""

    def __init__(
        self,
        cfg,
        conn,
        scoring,
        guard,
        adapters: dict[Platform, "object"],
        sm_round,
        sm_draft,
        circuit_breaker=None,
        notifier=None,
    ):
        self.cfg = cfg
        self.conn = conn
        self.scoring = scoring
        self.guard = guard
        self.adapters = adapters
        self.sm_round = sm_round
        self.sm_draft = sm_draft
        self.cb = circuit_breaker
        self.notifier = notifier

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def run(
        self,
        round_id: int,
        topic_count: int = 5,
        platform_values: Optional[list[str]] = None,
        save_drafts: bool = True,
    ) -> dict:
        """执行一轮。返回结构化执行报告（不抛业务异常，失败写进 report）。"""
        round_row = self.conn.execute(
            "SELECT status, goal_id, round_number FROM spiral_rounds WHERE id = ?",
            (round_id,),
        ).fetchone()
        if not round_row:
            raise ValueError(f"轮次 {round_id} 不存在")
        if round_row["status"] != "created":
            raise ValueError(
                f"轮次 {round_id} 当前状态为 {round_row['status']}，只有 created 状态可执行"
            )

        platforms = self._resolve_platforms(platform_values)
        if not platforms:
            raise ValueError("没有可用平台：请检查 config.platforms 的 enabled 与请求参数")

        report: dict = {
            "round_id": round_id,
            "round_number": round_row["round_number"],
            "started_at": datetime.now().isoformat(),
            "platforms": [p.value for p in platforms],
            "topic_count_requested": topic_count,
            "steps": [],
            "contents_created": 0,
            "drafts_created": 0,
            "drafts_saved": 0,
            "drafts_failed": 0,
            "guard_rejected": 0,
            "retry_counts": {},
            "errors": [],
            "final_status": None,
        }

        if not self.sm_round.transition(round_id, "producing", "执行器启动"):
            raise ValueError(f"轮次 {round_id} 无法进入 producing")

        task_run_id = self._start_task_run(round_id)

        try:
            # ① + ② 选题读取与评分（G 节点）
            scored = self._select_topics(topic_count, report)
            if not scored:
                report["errors"].append("选题库无可用选题")
                self.sm_round.transition(round_id, "failed", "选题库无可用选题")
                report["final_status"] = "failed"
                self._finish_task_run(task_run_id, "failed", "选题库无可用选题")
                return report

            # §19 母计划签名（R18）：选题分布确定后立即固化，改计划可追溯
            self._record_plan_signature(round_id, topic_count, platforms, scored, report)

            # ③④⑤⑥ 每选题 × 每平台产出内容
            content_rows = await self._produce_contents(
                round_id, scored, platforms, report,
            )

            # ⑦ 存草稿
            if save_drafts:
                await self._save_all_drafts(content_rows, round_id, report)

            # 收尾状态
            self._finalize_round(round_id, report, save_drafts)

        except Exception as exc:  # noqa: BLE001 — 执行器兜底，必须留痕
            logger.exception("轮次 %s 执行异常", round_id)
            report["errors"].append(f"执行异常: {exc}")
            self.sm_round.transition(round_id, "failed", f"执行异常: {exc}")
            report["final_status"] = "failed"

        if report["final_status"] == "failed":
            self._finish_task_run(task_run_id, "failed", "；".join(report["errors"]) or None)
        else:
            self._finish_task_run(task_run_id, "success")

        report["finished_at"] = datetime.now().isoformat()
        logger.info(
            "🏁 轮次 %s 执行完成: 内容 %d / 草稿 %d（成功 %d 失败 %d）状态=%s",
            round_id, report["contents_created"], report["drafts_created"],
            report["drafts_saved"], report["drafts_failed"], report["final_status"],
        )
        return report

    # ------------------------------------------------------------------
    # task_runs 留痕（断点恢复依据）+ 母计划签名
    # ------------------------------------------------------------------

    def _start_task_run(self, round_id: int) -> Optional[int]:
        try:
            cur = self.conn.execute(
                "INSERT INTO task_runs (task_type, biz_id, status, attempt, started_at) "
                "VALUES ('round_pipeline', ?, 'running', 1, ?)",
                (str(round_id), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self.conn.commit()
            return cur.lastrowid
        except Exception:  # noqa: BLE001 — 留痕失败不能中断主流程
            logger.exception("task_runs 写入失败 round=%s", round_id)
            return None

    def _finish_task_run(
        self, task_run_id: Optional[int], status: str, error: Optional[str] = None,
    ) -> None:
        if task_run_id is None:
            return
        try:
            self.conn.execute(
                "UPDATE task_runs SET status = ?, finished_at = ?, error_msg = ? WHERE id = ?",
                (
                    status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    error[:500] if error else None, task_run_id,
                ),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("task_runs 收尾失败 id=%s", task_run_id)

    def _record_plan_signature(
        self, round_id: int, topic_count: int,
        platforms: list[Platform], scored: list, report: dict,
    ) -> None:
        try:
            allocation: dict[str, int] = {}
            for s in scored:
                dim = s.topic.dimension or "未分类"
                allocation[dim] = allocation.get(dim, 0) + 1
            pvm = PlanVersionManager(self.cfg, self.conn)
            plan = pvm.build_plan(
                topic_count_per_platform=topic_count,
                platforms=[p.value for p in platforms],
                dimension_allocation=allocation,
            )
            signature = pvm.record(round_id, plan, author="round_executor")
            report["plan_signature"] = signature
            report["steps"].append({
                "step": "plan_signature",
                "signature": signature[:12],
                "dimension_allocation": allocation,
            })
        except Exception:  # noqa: BLE001 — 签名失败不应阻断产出
            logger.exception("母计划签名记录失败 round=%s", round_id)
            report["errors"].append("母计划签名记录失败")

    # ------------------------------------------------------------------
    # ① ② 选题
    # ------------------------------------------------------------------

    def _select_topics(self, topic_count: int, report: dict) -> list:
        try:
            items, kb_report = KbReader(self.cfg).read()
        except FileNotFoundError as exc:
            report["errors"].append(f"选题库读取失败: {exc}")
            return []
        report["steps"].append({
            "step": "kb_read",
            "count": len(items),
            "file_type": kb_report.get("file_type"),
        })
        if not items:
            return []

        scored = sorted(
            self.scoring.score_topics(items),
            key=lambda s: s.total_score,
            reverse=True,
        )
        # 选题库可能存在同 id 重复行（contents 有 UNIQUE(round_id, topic_id, platform)），
        # 先按 id 去重再截取，否则同一选题会撞唯一约束导致整轮失败
        seen_ids: set[str] = set()
        unique: list = []
        for s in scored:
            if s.topic.id in seen_ids:
                continue
            seen_ids.add(s.topic.id)
            unique.append(s)
        scored = unique[:topic_count]

        report["steps"].append({
            "step": "score",
            "mode": scored[0].mode if scored else None,
            "selected": [
                {
                    "topic_id": s.topic.id,
                    "title": s.topic.title,
                    "dimension": s.topic.dimension,
                    "score": round(s.total_score, 2),
                    "is_exploration": s.is_exploration,
                }
                for s in scored
            ],
        })
        return scored

    # ------------------------------------------------------------------
    # ③ ④ ⑤ ⑥ 内容产出（G2 串行拦截）
    # ------------------------------------------------------------------

    async def _produce_contents(
        self, round_id: int, scored: list, platforms: list[Platform], report: dict,
    ) -> list[dict]:
        output_dir = self.cfg.resolve_path(self.cfg.paths.output_dir)
        rows: list[dict] = []
        topic_ids: list[str] = []
        variant_summary: list[dict] = []

        for st in scored:
            topic = st.topic
            topic_ids.append(topic.id)
            self._upsert_topic_index(topic)

            for platform in platforms:
                if self._platform_broken(platform):
                    report["errors"].append(
                        f"{platform.display_name} {_SKIP_REASON_CIRCUIT}"
                    )
                    continue

                # ③ 文案生成 + ④ G2 终极目标检查（R2：FAIL 丢弃重生成，最多 2 次；
                #    ERROR 不能默认通过）
                accepted = None
                reject_reason: Optional[str] = None
                for attempt in range(MAX_GUARD_RETRIES + 1):
                    # LLM 为阻塞调用，放到线程池避免卡住事件循环
                    title, body, tags = await asyncio.to_thread(
                        generate_copy_content,
                        topic.title, topic.summary, topic.dimension, platform, self.cfg,
                        self.conn, topic.id, round_id,
                        reject_reason,
                    )

                    try:
                        outcome = self.guard.check_content(title, body, tags)
                    except Exception as exc:  # noqa: BLE001 — 检查器异常不能默认通过
                        logger.exception("G2 检查器异常 platform=%s", platform.value)
                        outcome = CheckOutcome(
                            result=CheckResult.ERROR,
                            reason=f"检查器异常: {exc}",
                        )

                    if outcome.passes:
                        accepted = (title, body, tags, outcome)
                        break

                    # D8：模板降级（无 LLM，无法自我改写）下 WARN 会确定性重复相同文案，
                    # 重试只会第 3 次仍 WARN → 被当 FAIL 确定性丢弃。故无 LLM 时 WARN 不再丢弃，
                    # 改为"采用 + 告警留痕"；有 LLM 时仍走重检（能改写，命中 R2 重检要求）。
                    if outcome.result == CheckResult.WARN and not _llm_available(self.cfg):
                        logger.warning(
                            "⚠️ G2 WARN 采用+告警（非丢弃）%s 第 %d 次: %s",
                            platform.value, attempt + 1, outcome.reason,
                        )
                        accepted = (title, body, tags, outcome)
                        break

                    # 未通过：计数 + 留痕，然后携带原因回 G1 重写
                    report["guard_rejected"] += 1
                    report["steps"].append({
                        "step": "guard_reject",
                        "platform": platform.value,
                        "topic_id": topic.id,
                        "result": outcome.result.value,
                        "reason": outcome.reason,
                        "attempt": attempt,
                    })
                    logger.warning(
                        "🚨 G2 拦截（%s）%s 第 %d 次: %s",
                        outcome.result.value, platform.value, attempt + 1, outcome.reason,
                    )
                    reject_reason = outcome.reason

                if accepted is None:
                    # 重试超限：该平台变体不发草稿，通知落地
                    report["retry_counts"][f"{platform.value}/{topic.id}"] = MAX_GUARD_RETRIES
                    msg = (
                        f"{platform.display_name} 平台「{topic.title[:20]}」"
                        f"重试 {MAX_GUARD_RETRIES} 次仍不合规，已丢弃"
                    )
                    report["errors"].append(msg)
                    self._notify_guard_give_up(platform, topic.title, msg)
                    continue

                title, body, tags, outcome = accepted

                # ⑤ 图片卡片
                image_path = await asyncio.to_thread(
                    make_card, title, topic.dimension, output_dir, platform,
                )

                # ⑥ 写 contents
                content_id = f"content-{uuid.uuid4().hex[:12]}"
                self.conn.execute(
                    "INSERT INTO contents (id, round_id, topic_id, platform, title, body, "
                    "tags, image_paths, word_count, card_style, guard_outcome, guard_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        content_id, round_id, topic.id, platform.value, title, body,
                        json.dumps(tags, ensure_ascii=False),
                        json.dumps([str(image_path)], ensure_ascii=False),
                        len(body), "knowledge", outcome.result.value, outcome.reason,
                    ),
                )

                # 写 drafts（幂等：同 content 不会重复，UNIQUE(content_id, platform)）
                draft_id = f"draft-{uuid.uuid4().hex[:12]}"
                self.conn.execute(
                    "INSERT INTO drafts (id, content_id, platform, status) "
                    "VALUES (?, ?, ?, 'draft_pending')",
                    (draft_id, content_id, platform.value),
                )
                self.conn.commit()

                rows.append({
                    "content_id": content_id,
                    "draft_id": draft_id,
                    "platform": platform,
                    "title": title,
                    "body": body,
                    "tags": tags,
                    "image_path": str(image_path),
                })
                report["contents_created"] += 1
                report["drafts_created"] += 1

            variant_summary.append({
                "topic_id": topic.id,
                "platforms": [p.value for p in platforms],
            })

        report["steps"].append({
            "step": "produce",
            "contents": report["contents_created"],
            "guard_rejected": report["guard_rejected"],
        })

        self.conn.execute(
            "UPDATE spiral_rounds SET topic_ids = ?, content_variants = ?, guard_rejections = ? "
            "WHERE id = ?",
            (
                json.dumps(topic_ids, ensure_ascii=False),
                json.dumps(variant_summary, ensure_ascii=False),
                report["guard_rejected"],
                round_id,
            ),
        )
        self.conn.commit()
        return rows

    # ------------------------------------------------------------------
    # ⑦ 存草稿（R1 + R16）
    # ------------------------------------------------------------------

    async def _save_all_drafts(self, rows: list[dict], round_id: int, report: dict) -> None:
        saved_draft_ids: list[str] = []

        for row in rows:
            platform: Platform = row["platform"]
            adapter = self.adapters.get(platform)
            if adapter is None:
                report["drafts_failed"] += 1
                self._mark_draft_failed(row["draft_id"], "适配器未注册")
                continue

            if self._platform_broken(platform):
                report["drafts_failed"] += 1
                self._mark_draft_failed(row["draft_id"], _SKIP_REASON_CIRCUIT)
                continue

            # R16 幂等：content_id + platform 已有 draft_saved 就跳过（不按本轮新 draft id 判断）
            exist = self.conn.execute(
                "SELECT id, status FROM drafts WHERE content_id = ? AND platform = ? "
                "AND status IN ('draft_saved','user_published','published') "
                "ORDER BY rowid DESC LIMIT 1",
                (row["content_id"], platform.value),
            ).fetchone()
            if exist:
                logger.info(
                    "幂等命中：content=%s platform=%s 已存草稿（%s），跳过",
                    row["content_id"], platform.value, exist["status"],
                )
                saved_draft_ids.append(exist["id"])
                continue

            payload = ContentToPublish(
                content_id=row["content_id"],
                platform=platform,
                title=row["title"],
                body=row["body"],
                image_paths=[row["image_path"]],
                tags=row["tags"],
            )
            try:
                result = await adapter.save_draft(payload)
            except Exception as exc:  # noqa: BLE001 — 单篇失败不影响整轮
                logger.exception("存草稿异常 platform=%s", platform.value)
                report["drafts_failed"] += 1
                self._mark_draft_failed(row["draft_id"], f"存草稿异常: {exc}")
                self._notify_failed(platform, row["title"], f"存草稿异常: {exc}")
                continue

            if result.success:
                self.sm_draft.transition(
                    row["draft_id"], "draft_saved",
                    reason="Playwright 存草稿成功",
                    extra_updates={"draft_id": result.platform_draft_id},
                )
                report["drafts_saved"] += 1
                saved_draft_ids.append(row["draft_id"])
                if self.notifier:
                    self.notifier.emit(
                        "draft_saved",
                        f"{platform.display_name} 草稿已保存：{row['title'][:30]}",
                    )
            elif result.needs_human_verify:
                # —— R17 / P0-3 新增：按钮点了但无成功证据，需人工核实 ——
                self.sm_draft.transition(
                    row["draft_id"], "failed",
                    reason="needs_human_verify: 存草稿后未检测到平台成功信号",
                    extra_updates={"error_msg": result.error_msg or "needs_human_verify"},
                )
                report.setdefault("drafts_needs_human_verify", 0)
                report["drafts_needs_human_verify"] += 1
                report["drafts_failed"] += 1
                logger.warning(
                    "存草稿 needs_human_verify platform=%s draft=%s draft_id_missing=%s",
                    platform.value, row["draft_id"], result.platform_draft_id_missing,
                )
                if self.notifier:
                    self.notifier.emit(
                        "needs_human_verify",
                        f"{platform.display_name} 存草稿结果不确定（需人工核实）：{row['title'][:30]}",
                    )
            else:
                # R6：页面信号 / 登录态失效 → 真正触发熔断（此前只记日志，熔断器形同虚设）
                self._report_circuit_signal(platform, result)
                report["drafts_failed"] += 1
                self._mark_draft_failed(
                    row["draft_id"], result.error_msg or "存草稿失败",
                )
                self._notify_failed(
                    platform, row["title"], result.error_msg or "存草稿失败",
                )

        self.conn.execute(
            "UPDATE spiral_rounds SET draft_ids = ? WHERE id = ?",
            (json.dumps(saved_draft_ids, ensure_ascii=False), round_id),
        )
        self.conn.commit()
        report["steps"].append({
            "step": "save_drafts",
            "saved": report["drafts_saved"],
            "failed": report["drafts_failed"],
        })

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _resolve_platforms(self, platform_values: Optional[list[str]]) -> list[Platform]:
        enabled = set(self.cfg.platforms.enabled_keys())
        wanted = enabled if not platform_values else (enabled & set(platform_values))
        result: list[Platform] = []
        for p in Platform:
            if p.value in wanted and p in self.adapters:
                result.append(p)
        return result

    def _platform_broken(self, platform: Platform) -> bool:
        if self.cb is None:
            return False
        try:
            return bool(self.cb.is_circuit_broken(platform))
        except Exception:  # noqa: BLE001 — 熔断判断失败不阻塞生产
            logger.exception("熔断状态读取失败 platform=%s", platform.value)
            return False

    def _report_circuit_signal(self, platform: Platform, result) -> None:
        """把存草稿结果里的风险信号真正上报熔断器（R6）。

        两种情况：
        - 页面出现验证码/限流/拦截/设备验证 → 用对应 reason 熔断
        - 登录态失效 → LOGIN_EXPIRED 熔断（恢复需 3 步验证）
        """
        if self.cb is None:
            return
        signal = getattr(result, "cb_signal", None)
        if signal is None and getattr(result, "login_required", False):
            signal = CircuitBreakerReason.LOGIN_EXPIRED
        if signal is None:
            return
        try:
            self.cb.trigger(platform, signal)
        except Exception:  # noqa: BLE001 — 熔断上报失败不阻塞本轮收尾
            logger.exception("熔断上报失败 platform=%s signal=%s", platform.value, signal)

    def _upsert_topic_index(self, topic) -> None:
        """把选题写入 topic_index（contents.topic_id 有外键）。"""
        self.conn.execute(
            "INSERT INTO topic_index (id, dimension, source, title, date, total_uses) "
            "VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(id) DO UPDATE SET total_uses = COALESCE(total_uses, 0) + 1",
            (topic.id, topic.dimension, topic.source, topic.title, topic.date),
        )
        self.conn.commit()

    def _mark_draft_failed(self, draft_id: str, reason: str) -> None:
        self.sm_draft.transition(
            draft_id, "failed", reason=reason, extra_updates={"error_msg": reason},
        )

    def _notify_failed(self, platform: Platform, title: str, reason: str) -> None:
        """存草稿失败通知（error 级，落 notifications 表）。"""
        if not self.notifier:
            return
        self.notifier.emit(
            "draft_failed",
            f"{platform.display_name} 存草稿失败：{(title or '')[:30]} — {reason}",
            platform=platform.value,
        )

    def _notify_guard_give_up(self, platform: Platform, title: str, msg: str) -> None:
        """G2 重试超限通知（error 级）：该平台变体已丢弃，不发草稿。"""
        if not self.notifier:
            return
        self.notifier.emit(
            "draft_failed",
            msg or f"{platform.display_name} G2 重试超限，变体已丢弃",
            platform=platform.value,
        )

    def _finalize_round(self, round_id: int, report: dict, save_drafts: bool = True) -> None:
        """全部草稿落定 → awaiting_publish；全军覆没 → failed；仍有待存则留在 producing。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN d.status = 'draft_saved' THEN 1 ELSE 0 END) AS saved, "
            "SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN d.status = 'draft_pending' THEN 1 ELSE 0 END) AS pending "
            "FROM drafts d JOIN contents c ON d.content_id = c.id WHERE c.round_id = ?",
            (round_id,),
        ).fetchone()

        total = row["total"] or 0
        saved = row["saved"] or 0
        failed = row["failed"] or 0
        pending = row["pending"] or 0

        if total == 0:
            self.sm_round.transition(round_id, "failed", "本轮无任何内容产出")
            report["final_status"] = "failed"
            return

        if pending > 0:
            # 内容已产出但草稿还没存 → 留在 producing
            logger.info("轮次 %s 仍有 %d 篇待存草稿，保持 producing", round_id, pending)
            report["final_status"] = "producing"
            if save_drafts:
                report["errors"].append(f"{pending} 篇草稿仍处于 draft_pending（存草稿未落定）")
            else:
                # 干跑（save_drafts=false）不是错误，只是本轮没走存草稿
                report["steps"].append({
                    "step": "dry_run",
                    "note": f"{pending} 篇草稿停在 draft_pending（本次未执行存草稿）",
                })
            return

        if saved == 0 and failed == total:
            self.sm_round.transition(round_id, "failed", "全部草稿存失败")
            report["final_status"] = "failed"
            return

        self.sm_round.transition(round_id, "awaiting_publish", "草稿已存，等待用户发布")
        report["final_status"] = "awaiting_publish"
