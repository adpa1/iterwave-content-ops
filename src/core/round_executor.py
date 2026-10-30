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
from .kb_reader import KbReader  # noqa: F401 — 兼容旧引用
from .topic_pool import load_pool
from .persona import effective_persona
from .hot_topics import _sensitive_topic_hits
from .orchestration import Orchestration, step_status_from_report
from .content_pipeline import generate_copy_content, make_card_set, _llm_available
from .plan_version import PlanVersionManager
from .guard.ultimate_goal_checker import CheckOutcome, CheckResult
from ..adapters.base import Platform, ContentToPublish
from ..adapters.base import DraftStatus, CircuitBreakerReason

logger = logging.getLogger(__name__)

# 平台未启用 / 熔断时不给该平台产出内容
_SKIP_REASON_CIRCUIT = "平台熔断中，跳过本轮产出"


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
        topic_count: Optional[int] = None,
        platform_values: Optional[list[str]] = None,
        save_drafts: Optional[bool] = None,
        feedback=None,
    ) -> dict:
        """执行一轮。返回结构化执行报告（不抛业务异常，失败写进 report）。

        feedback：上一轮复盘结论（StrategyFeedback）。不传且 autogrow.apply_feedback 开启时
        自动从最近复盘构建 —— 手动轮与自动轮都吃复盘结论，闭环不依赖谁来开轮。
        """
        round_row = self.conn.execute(
            "SELECT status, goal_id, round_number FROM spiral_rounds WHERE id = ?",
            (round_id,),
        ).fetchone()
        if not round_row:
            raise ValueError(f"轮次 {round_id} 不存在")
        current_status = round_row["status"]
        if current_status == "failed":
            # 只允许没有产生任何内容/草稿的失败轮次重试，避免重复生成或覆盖已有交付物。
            existing_contents = self.conn.execute(
                "SELECT COUNT(*) FROM contents WHERE round_id = ?", (round_id,)
            ).fetchone()[0]
            existing_drafts = self.conn.execute(
                "SELECT COUNT(*) FROM drafts d JOIN contents c ON c.id = d.content_id WHERE c.round_id = ?",
                (round_id,),
            ).fetchone()[0]
            if existing_contents or existing_drafts:
                raise ValueError("本轮失败前已产生内容或草稿，为避免重复交付，请修复问题后新建一轮")
            if not self.sm_round.transition(round_id, "created", "用户发起安全重试（无已生成内容）"):
                raise ValueError("失败轮次无法重置，请新建一轮")
        elif current_status != "created":
            raise ValueError(
                f"轮次 {round_id} 当前状态为 {current_status}，仅新建或可安全重试的失败轮次可执行"
            )

        # 执行前预检：选题池为空时不进入 producing，不制造一次必然失败的轮次。
        available_topics, pool_report = load_pool(self.cfg, self.conn)
        if not available_topics:
            detail = pool_report.get("kb_error") or "选题库当前没有可用选题"
            raise ValueError(f"运行前检查未通过：{detail}。请先补充或恢复选题，再运行本轮")

        # 母计划 v2：未显式传参时按母计划执行（选题数 / 平台 / 维度配额 / 平台分项篇数）
        self._pvm = PlanVersionManager(self.cfg, self.conn)
        self._plan, self._plan_sig, _at, self._plan_source = self._pvm.load_plan()
        if topic_count is None:
            topic_count = int(self._plan.get("topic_count_per_platform") or self.cfg.plan_suggestion.default_topic_count)
        if platform_values is None and self._plan_source == "plan_settings":
            platform_values = list(self._plan.get("enabled_platforms") or []) or None
        # Agent 编排：存草稿 / 出图 / 自动排期按编排开关运行
        self._orch = Orchestration.load(self.conn)
        if save_drafts is None:
            save_drafts = self._orch.on("save_draft")
        else:
            save_drafts = bool(save_drafts) and self._orch.on("save_draft")

        platforms = self._resolve_platforms(platform_values)
        if not platforms:
            raise ValueError("没有可用平台：请检查 config.platforms 的 enabled 与请求参数")

        # 运行前硬门：本轮每个目标平台都必须有可比较的账号定位关键词。
        missing_persona = [
            p.value for p in platforms
            if not effective_persona(self.cfg, p.value).get("persona_keywords")
        ]
        if missing_persona:
            labels = "、".join(Platform(p).display_name for p in missing_persona)
            raise ValueError(
                f"运行前检查未通过：{labels} 未配置账号定位关键词，无法判断内容是否符合账号定位。"
                "请先到「目标与策略」补齐对应平台画像"
            )

        report: dict = {
            "round_id": round_id,
            "round_number": round_row["round_number"],
            "started_at": datetime.now().isoformat(),
            "platforms": [p.value for p in platforms],
            "topic_count_requested": topic_count,
            "plan_source": self._plan_source,
            "orchestration": [s["id"] for s in self._orch.steps if s["enabled"]],
            "steps": [],
            "contents_created": 0,
            "drafts_created": 0,
            "degraded_visuals": 0,
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
            # 复盘反馈也属于执行流程；若读取/解析异常，必须进入统一失败收尾，
            # 不能让轮次停在 producing 且 task_run 永远保持 running。
            self._feedback = self._resolve_feedback(round_id, feedback, report)

            # ① + ② 选题读取与评分（G 节点）
            scored = self._select_topics(topic_count, report)
            if not scored:
                report["errors"].append("选题库无可用选题")
                self.sm_round.transition(round_id, "failed", "选题库无可用选题")
                report["final_status"] = "failed"
                self._finish_task_run(task_run_id, "failed", "选题库无可用选题")
                report["finished_at"] = datetime.now().isoformat()
                report["step_status"] = step_status_from_report(report, self._orch)
                try:
                    self.conn.execute(
                        "UPDATE spiral_rounds SET run_report = ? WHERE id = ?",
                        (json.dumps(report, ensure_ascii=False, default=str), round_id),
                    )
                    self.conn.commit()
                except Exception:  # noqa: BLE001 — 失败报告尽量持久化，但不遮盖主错误
                    logger.exception("空选题失败报告写入失败 round=%s", round_id)
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
                # ⑧ 自动排期（编排开启 + 母计划配置了平台发布时段）
                if self._orch.on("auto_schedule"):
                    self._auto_schedule(round_id, report)

            # 收尾状态
            self._finalize_round(round_id, report, save_drafts)

        except Exception as exc:  # noqa: BLE001 — 执行器兜底，必须留痕
            logger.exception("轮次 %s 执行异常", round_id)
            report["errors"].append(f"执行异常: {exc}")
            report["final_status"] = "failed"
            # 状态迁移本身也可能因数据库/状态机异常失败；不能让它覆盖原始异常，
            # 否则下方 task_runs 收尾和 run_report 持久化都不会执行。
            try:
                transitioned = self.sm_round.transition(
                    round_id, "failed", f"执行异常: {exc}",
                )
                if not transitioned:
                    report["errors"].append("失败收尾：轮次状态未能迁移到 failed")
            except Exception as cleanup_exc:  # noqa: BLE001 — 尽力收尾，不遮盖原始错误
                logger.exception("轮次失败状态收尾异常 round=%s", round_id)
                report["errors"].append(f"失败状态收尾异常: {cleanup_exc}")

        if report["final_status"] == "failed":
            self._finish_task_run(task_run_id, "failed", "；".join(report["errors"]) or None)
        else:
            self._finish_task_run(task_run_id, "success")

        report["finished_at"] = datetime.now().isoformat()
        report["step_status"] = step_status_from_report(report, self._orch)
        try:
            self.conn.execute(
                "UPDATE spiral_rounds SET run_report = ? WHERE id = ?",
                (json.dumps(report, ensure_ascii=False, default=str), round_id),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("run_report 写入失败 round=%s", round_id)
        logger.info(
            "🏁 轮次 %s 执行完成: 内容 %d / 草稿 %d（成功 %d 失败 %d）状态=%s",
            round_id, report["contents_created"], report["drafts_created"],
            report["drafts_saved"], report["drafts_failed"], report["final_status"],
        )
        return report

    async def save_pending_drafts(self, round_id: int) -> dict:
        """恢复存草稿未执行的轮次，不重新生成内容。

        只处理已经通过质量门、持久化在 contents/drafts 中且仍为 draft_pending 的内容。
        所有发布动作仍由用户在平台完成；此方法只调用 adapter.save_draft。
        """
        round_row = self.conn.execute(
            "SELECT status, round_number, run_report FROM spiral_rounds WHERE id = ?",
            (round_id,),
        ).fetchone()
        if not round_row:
            raise ValueError(f"轮次 {round_id} 不存在")
        if round_row["status"] != "producing":
            raise ValueError(
                f"轮次 {round_id} 当前状态为 {round_row['status']}，只有 producing 状态可恢复存草稿"
            )

        self._orch = Orchestration.load(self.conn)
        if not self._orch.on("save_draft"):
            raise ValueError("Agent 编排已关闭「存草稿」步骤，请先启用后再恢复")

        pending_rows = self.conn.execute(
            "SELECT d.id AS draft_id, d.content_id, d.platform, "
            "c.title, c.body, c.tags, c.image_paths, c.review_rejected, "
            "c.review_rejection_reason "
            "FROM drafts d JOIN contents c ON c.id = d.content_id "
            "WHERE c.round_id = ? AND d.status = 'draft_pending' "
            "ORDER BY c.rowid, d.rowid",
            (round_id,),
        ).fetchall()
        if not pending_rows:
            raise ValueError(f"轮次 {round_id} 没有待存草稿")

        rejected_count = sum(1 for item in pending_rows if bool(item["review_rejected"]))
        pending_rows = [item for item in pending_rows if not bool(item["review_rejected"])]
        if not pending_rows:
            raise ValueError(
                f"轮次 {round_id} 的 {rejected_count} 篇待存内容均已被人工驳回；"
                "请修改文案、重新过目标检查并提交审核，或取消本轮。未调用任何平台存草稿操作"
            )

        if self.guard is None:
            raise ValueError("质量门检查器不可用，已拒绝恢复存草稿")

        rows: list[dict] = []
        guard_failures: list[str] = []
        llm_available = _llm_available(self.cfg)
        for item in pending_rows:
            try:
                tags = json.loads(item["tags"]) if item["tags"] else []
            except (TypeError, ValueError):
                tags = []
            if not isinstance(tags, list):
                tags = []
            tags = [str(tag) for tag in tags]

            try:
                image_paths = json.loads(item["image_paths"]) if item["image_paths"] else []
            except (TypeError, ValueError):
                image_paths = []
            if not isinstance(image_paths, list):
                image_paths = []

            platform = Platform(item["platform"])
            title = item["title"] or ""
            body = item["body"] or ""
            try:
                # 必须按当前数据库内容重跑质量门；绝不能假定原文案的通过结果仍有效。
                outcome = self.guard.check_content(
                    title, body, tags, platform=platform.value,
                )
            except Exception as exc:  # noqa: BLE001 — 检查器异常必须 fail closed
                logger.exception(
                    "恢复存草稿前质量门异常 round=%s content=%s",
                    round_id, item["content_id"],
                )
                outcome = CheckOutcome(
                    result=CheckResult.ERROR,
                    reason=f"恢复存草稿前质量门异常：{exc}",
                )

            # 跟初始生产保持一致：只有模板降级（无 LLM）的 WARN 可带告警采用。
            accepted = outcome.passes or (
                outcome.result == CheckResult.WARN and not llm_available
            )
            self.conn.execute(
                "UPDATE contents SET guard_outcome = ?, guard_reason = ? WHERE id = ?",
                (outcome.result.value, outcome.reason, item["content_id"]),
            )
            if not accepted:
                guard_failures.append(
                    f"「{title[:40]}」检查结果为 {outcome.result.value}：{outcome.reason or '未通过质量门'}"
                )
            elif outcome.result == CheckResult.WARN:
                logger.warning(
                    "恢复存草稿接受无 LLM 模式的 G2 WARN round=%s content=%s reason=%s",
                    round_id, item["content_id"], outcome.reason,
                )

            rows.append({
                "draft_id": item["draft_id"],
                "content_id": item["content_id"],
                "platform": platform,
                "title": title,
                "body": body,
                "tags": tags,
                "image_paths": image_paths,
                "image_path": str(image_paths[0]) if image_paths and image_paths[0] else None,
            })

        # 全部待存内容都重新检查后再执行任何平台副作用；任意一篇未通过就整体阻断。
        self.conn.commit()
        if guard_failures:
            raise ValueError(
                "恢复存草稿已中止，存在未通过当前质量门的内容："
                + "；".join(guard_failures[:5])
                + "。请修改文案后重新检查，再继续存草稿"
            )

        report: dict = {
            "round_id": round_id,
            "round_number": round_row["round_number"],
            "started_at": datetime.now().isoformat(),
            "steps": [],
            "contents_created": 0,
            "drafts_created": len(rows),
            "drafts_attempted": len(rows),
            "drafts_saved": 0,
            "drafts_failed": 0,
            "guard_rejected": 0,
            "errors": [],
            "final_status": "producing",
            "operation": "resume_save_drafts",
        }

        await self._save_all_drafts(rows, round_id, report)
        self._finalize_round(round_id, report, save_drafts=True)
        report["pending_drafts"] = int(self.conn.execute(
            "SELECT COUNT(*) FROM drafts d JOIN contents c ON c.id = d.content_id "
            "WHERE c.round_id = ? AND d.status = 'draft_pending'",
            (round_id,),
        ).fetchone()[0])
        report["finished_at"] = datetime.now().isoformat()

        # 保留原有生产报告，并把恢复操作作为历史事件追加；移除当前态的 dry_run 标记，
        # 避免工作流仍被展示为“存草稿已跳过”。
        previous: dict = {}
        try:
            parsed = json.loads(round_row["run_report"] or "{}")
            if isinstance(parsed, dict):
                previous = parsed
        except (TypeError, ValueError):
            previous = {}
        previous["steps"] = [
            step for step in previous.get("steps", [])
            if isinstance(step, dict) and step.get("step") != "dry_run"
        ]
        # 汇总步骤名沿用编排诊断使用的 save_drafts，保证恢复后不再显示为 skipped。
        previous["steps"].append({
            "step": "save_drafts",
            "saved": int(previous.get("drafts_saved") or 0) + report["drafts_saved"],
            "failed": int(previous.get("drafts_failed") or 0) + report["drafts_failed"],
        })
        previous["steps"].append({
            "step": "resume_save_drafts",
            "attempted": report["drafts_attempted"],
            "saved": report["drafts_saved"],
            "failed": report["drafts_failed"],
            "pending": report["pending_drafts"],
        })
        previous.setdefault("resume_history", []).append(report)
        previous["drafts_saved"] = int(previous.get("drafts_saved") or 0) + report["drafts_saved"]
        previous["drafts_failed"] = int(previous.get("drafts_failed") or 0) + report["drafts_failed"]
        previous["final_status"] = report["final_status"]
        previous["finished_at"] = report["finished_at"]
        previous["step_status"] = step_status_from_report(previous, self._orch)
        self.conn.execute(
            "UPDATE spiral_rounds SET run_report = ? WHERE id = ?",
            (json.dumps(previous, ensure_ascii=False, default=str), round_id),
        )
        self.conn.commit()
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
            if getattr(self, "_plan_source", None) == "plan_settings":
                # 按母计划执行：轮次继承母计划签名（R18 下游与母计划一致，改计划才失效）
                plan = self._plan
            else:
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
                "planned_allocation": (getattr(self, "_plan", {}) or {}).get("dimension_allocation", {}),
            })
        except Exception:  # noqa: BLE001 — 签名失败不应阻断产出
            logger.exception("母计划签名记录失败 round=%s", round_id)
            report["errors"].append("母计划签名记录失败")

    # ------------------------------------------------------------------
    # ① ② 选题
    # ------------------------------------------------------------------

    def _resolve_feedback(self, round_id: int, feedback, report: dict):
        """复盘结论 → 本轮。失败只留痕不阻断（没有反馈 = 退回纯评分，不是错误）。"""
        ag = getattr(self.cfg, "autogrow", None)
        if feedback is None and ag is not None and ag.apply_feedback:
            try:
                from .autogrow import build_feedback
                feedback = build_feedback(self.cfg, self.conn, self.guard)
            except Exception:  # noqa: BLE001
                logger.exception("复盘结论构建失败 round=%s（本轮按纯评分执行）", round_id)
                report["errors"].append("复盘结论构建失败，本轮未吃进上一轮建议")
                feedback = None
        if feedback is None:
            return None
        try:
            self.conn.execute(
                "UPDATE spiral_rounds SET applied_feedback = COALESCE(applied_feedback, ?) WHERE id = ?",
                (feedback.to_json(), round_id),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("applied_feedback 写入失败 round=%s", round_id)
        report["steps"].append({
            "step": "apply_feedback",
            "source_round_ids": feedback.source_round_ids,
            "dimension_bias": feedback.dimension_bias,
            "copy_hint_count": sum(len(v) for v in feedback.copy_hints.values()),
            "dropped_hints": len(feedback.dropped_hints),
        })
        return feedback

    def _hints_for(self, platform) -> Optional[list[str]]:
        fb = getattr(self, "_feedback", None)
        return fb.hints_for(platform.value) if fb is not None else None

    def _apply_dimension_quota(self, ranked: list, topic_count: int, report: dict) -> list:
        """母计划维度配额：每个维度先按评分取满配额（库里不够就有多少取多少并留痕），
        剩余名额按总分补齐。最终按总分排序，保证平台分项篇数取的是高分选题。"""
        alloc = {d: int(n) for d, n in ((getattr(self, "_plan", {}) or {}).get("dimension_allocation") or {}).items()
                 if int(n) > 0}
        if not alloc:
            return ranked[:topic_count]
        picked: list = []
        shortfall = {}
        for dim, n in alloc.items():
            cands = [s for s in ranked if s.topic.dimension == dim and s not in picked]
            take = cands[: n]
            if len(take) < n:
                shortfall[dim] = n - len(take)
            picked.extend(take)
        picked = sorted(picked, key=lambda s: s.total_score, reverse=True)[:topic_count]
        for s in ranked:
            if len(picked) >= topic_count:
                break
            if s not in picked:
                picked.append(s)
        picked.sort(key=lambda s: s.total_score, reverse=True)
        report["steps"].append({"step": "dimension_quota", "allocation": alloc, "shortfall": shortfall})
        if shortfall:
            report["errors"].append(
                "选题池维度不足，已按评分补齐：" + "、".join(f"{d} 缺 {n}" for d, n in shortfall.items())
            )
        return picked

    def _platform_quota(self, platform: Platform, default: int) -> int:
        plan = getattr(self, "_plan", None)
        if not plan or getattr(self, "_plan_source", None) != "plan_settings":
            return default
        return min(default, PlanVersionManager.platform_topic_count(plan, platform.value))

    def _auto_schedule(self, round_id: int, report: dict) -> None:
        """按母计划平台发布时段把本轮 draft_saved 草稿排进日历：从明天起，每个时段一篇，
        跳过已被占用的时段（同平台同一时间不排两篇）。只写 scheduled_publish_at，不发布（R1）。"""
        from datetime import timedelta
        per = (getattr(self, "_plan", {}) or {}).get("per_platform") or {}
        scheduled = 0
        for platform_key, conf in per.items():
            slots = conf.get("publish_slots") or []
            if not slots:
                continue
            drafts = self.conn.execute(
                "SELECT d.id FROM drafts d JOIN contents c ON d.content_id = c.id "
                "WHERE c.round_id = ? AND d.platform = ? AND d.status = 'draft_saved' "
                "AND d.scheduled_publish_at IS NULL ORDER BY d.id",
                (round_id, platform_key),
            ).fetchall()
            taken = {r[0] for r in self.conn.execute(
                "SELECT scheduled_publish_at FROM drafts WHERE platform = ? AND scheduled_publish_at IS NOT NULL",
                (platform_key,),
            )}
            day = datetime.now().date() + timedelta(days=1)
            queue = [r[0] for r in drafts]
            guard = 0
            while queue and guard < 366:
                for slot in slots:
                    if not queue:
                        break
                    at = f"{day.isoformat()} {slot}:00" if len(slot) == 5 else f"{day.isoformat()} {slot}"
                    if at in taken:
                        continue
                    self.conn.execute("UPDATE drafts SET scheduled_publish_at = ? WHERE id = ?", (at, queue.pop(0)))
                    taken.add(at)
                    scheduled += 1
                day += timedelta(days=1)
                guard += 1
        self.conn.commit()
        report["steps"].append({"step": "auto_schedule", "scheduled": scheduled})

    def _select_topics(self, topic_count: int, report: dict) -> list:
        items, kb_report = load_pool(self.cfg, self.conn)
        if kb_report.get("kb_error"):
            report["errors"].append(f"选题库读取失败: {kb_report['kb_error']}")
        report["steps"].append({
            "step": "kb_read",
            "count": len(items),
            "file_type": kb_report.get("file_type"),
            "kb_count": kb_report.get("kb_count"),
            "extra_count": kb_report.get("extra_count"),
            "excluded_count": kb_report.get("excluded_count"),
        })
        if not items:
            return []

        sensitive_items = [it for it in items if _sensitive_topic_hits(it.title)]
        if sensitive_items:
            report["steps"].append({
                "step": "sensitive_topic_gate",
                "status": "manual_review_required",
                "skipped": len(sensitive_items),
                "items": [
                    {"topic_id": it.id, "title": it.title,
                     "categories": _sensitive_topic_hits(it.title)}
                    for it in sensitive_items
                ],
            })
            report["errors"].append(
                f"{len(sensitive_items)} 条敏感选题已转人工确认，本轮不自动生成"
            )
        items = [it for it in items if not _sensitive_topic_hits(it.title)]
        if not items:
            return []

        scored = sorted(
            self.scoring.score_topics(items, feedback=getattr(self, "_feedback", None)),
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
        scored = self._apply_dimension_quota(unique, topic_count, report)

        report["steps"].append({
            "step": "score",
            "mode": scored[0].mode if scored else None,
            "selected": [
                {
                    "topic_id": s.topic.id,
                    "title": s.topic.title,
                    "dimension": s.topic.dimension,
                    "score": round(s.total_score, 2),
                    "feedback_bias": s.score_breakdown.get("strategy_feedback", {}).get("score", 0),
                    "is_exploration": s.is_exploration,
                }
                for s in scored
            ],
        })
        return scored

    # ------------------------------------------------------------------
    # ③ ④ ⑤ ⑥ 内容产出（G2 串行拦截）
    # ------------------------------------------------------------------

    def _card_footer(self, platform: Platform) -> str:
        from .content_pipeline import card_footer
        return card_footer(self.cfg, platform)

    async def _produce_contents(
        self, round_id: int, scored: list, platforms: list[Platform], report: dict,
    ) -> list[dict]:
        output_dir = self.cfg.resolve_path(self.cfg.paths.output_dir)
        rows: list[dict] = []
        topic_ids: list[str] = []
        variant_summary: list[dict] = []

        orch = getattr(self, "_orch", None)
        # IterWave 的交付物是图文内容，图片卡片属于必需步骤，不能被编排开关静默关闭。
        # 兼容旧配置：记录已强制启用，避免旧账号组生成没有任何图片的内容。
        make_cards = True
        if orch and not orch.on("image_card"):
            report["steps"].append({
                "step": "image_card",
                "status": "forced_enabled",
                "reason": "图文内容必须包含视觉素材；旧编排中的关闭设置不再跳过出图",
            })
        card_first = bool(orch and orch.card_before_gate and make_cards)
        for rank, st in enumerate(scored):
            topic = st.topic
            # 第二道独立防线：进入内容生产前再次检查原始选题标题。
            sensitive_hits = _sensitive_topic_hits(topic.title)
            if sensitive_hits:
                report["steps"].append({
                    "step": "sensitive_topic_gate",
                    "status": "manual_review_required",
                    "topic_id": topic.id,
                    "title": topic.title,
                    "categories": sensitive_hits,
                })
                continue
            topic_ids.append(topic.id)
            self._upsert_topic_index(topic)

            for platform in platforms:
                if rank >= self._platform_quota(platform, len(scored)):
                    continue  # 平台分项计划：本平台只做前 N 个选题
                if self._platform_broken(platform):
                    report["errors"].append(
                        f"{platform.display_name} {_SKIP_REASON_CIRCUIT}"
                    )
                    continue

                # ③ 文案生成 + ④ G2 终极目标检查（R2：FAIL 丢弃重生成，最多 2 次；
                #    ERROR 不能默认通过）
                accepted = None
                image_paths: list = []
                reject_reason: Optional[str] = None
                for attempt in range(self.cfg.copy_generation.max_guard_retries + 1):
                    # LLM 为阻塞调用，放到线程池避免卡住事件循环
                    title, body, tags = await asyncio.to_thread(
                        generate_copy_content,
                        topic.title, topic.summary, topic.dimension, platform, self.cfg,
                        self.conn, topic.id, round_id,
                        reject_reason,
                        self._hints_for(platform),
                    )
                    if card_first:
                        # 编排把出图排在质量门之前：每个候选版本都先出图再质检
                        image_paths = await asyncio.to_thread(
                            make_card_set, title, topic.dimension, body, output_dir, platform,
                            self._card_footer(platform),
                        )

                    try:
                        outcome = self.guard.check_content(title, body, tags, platform=platform.value)
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
                    report["retry_counts"][f"{platform.value}/{topic.id}"] = self.cfg.copy_generation.max_guard_retries
                    msg = (
                        f"{platform.display_name} 平台「{topic.title[:20]}」"
                        f"重试 {self.cfg.copy_generation.max_guard_retries} 次仍不合规，已丢弃"
                    )
                    report["errors"].append(msg)
                    self._notify_guard_give_up(platform, topic.title, msg)
                    continue

                title, body, tags, outcome = accepted

                # ⑤ 图片卡片（编排可关闭；可调到质量门之前——此时被拦重写的版本也会出图）
                if not card_first:
                    image_paths = []
                    if make_cards:
                        image_paths = await asyncio.to_thread(
                            make_card_set, title, topic.dimension, body, output_dir, platform,
                            self._card_footer(platform),
                        )

                # ⑥ 写 contents
                content_id = f"content-{uuid.uuid4().hex[:12]}"
                self.conn.execute(
                    "INSERT INTO contents (id, round_id, topic_id, platform, title, body, "
                    "tags, image_paths, word_count, card_style, guard_outcome, guard_reason, "
                    "visual_status, visual_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'degraded', ?)",
                    (
                        content_id, round_id, topic.id, platform.value, title, body,
                        json.dumps(tags, ensure_ascii=False),
                        json.dumps([str(p) for p in image_paths], ensure_ascii=False),
                        len(body), "knowledge", outcome.result.value, outcome.reason,
                        "仅使用本地模板卡片的文字排版与渐变背景，尚未选用正式视觉素材",
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
                    "image_paths": [str(p) for p in image_paths],
                })
                report["contents_created"] += 1
                report["degraded_visuals"] = int(report.get("degraded_visuals") or 0) + 1
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
                image_paths=row["image_paths"],
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
                from src.core.content_version import content_version_hash
                saved_version_hash = content_version_hash(
                    payload.title, payload.body, payload.tags, payload.image_paths,
                )
                self.sm_draft.transition(
                    row["draft_id"], "draft_saved",
                    reason="Playwright 存草稿成功",
                    extra_updates={
                        "draft_id": result.platform_draft_id,
                        "saved_content_hash": saved_version_hash,
                        "saved_platform_title": payload.title,
                    },
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

        # 恢复存草稿可能是分批执行；不能用本次成功的 ID 覆盖此前已存成功的草稿关联。
        round_row = self.conn.execute(
            "SELECT draft_ids FROM spiral_rounds WHERE id = ?", (round_id,)
        ).fetchone()
        existing_ids: list[str] = []
        try:
            decoded_ids = json.loads(round_row["draft_ids"] or "[]") if round_row else []
            if isinstance(decoded_ids, list):
                existing_ids = [str(value) for value in decoded_ids if value]
        except (TypeError, ValueError):
            logger.warning("轮次 %s 的 draft_ids 不可解析，保存本次可确认的关联", round_id)
        merged_ids = list(dict.fromkeys([*existing_ids, *saved_draft_ids]))
        self.conn.execute(
            "UPDATE spiral_rounds SET draft_ids = ? WHERE id = ?",
            (json.dumps(merged_ids, ensure_ascii=False), round_id),
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
        """按仍可执行的草稿收口：待存留 producing；有已保存草稿则等发布；无可发布内容则失败/取消。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN d.status = 'draft_saved' THEN 1 ELSE 0 END) AS saved, "
            "SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN d.status = 'draft_pending' THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN d.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled, "
            "SUM(CASE WHEN d.status = 'expired' THEN 1 ELSE 0 END) AS expired "
            "FROM drafts d JOIN contents c ON d.content_id = c.id WHERE c.round_id = ?",
            (round_id,),
        ).fetchone()

        total = row["total"] or 0
        saved = row["saved"] or 0
        failed = row["failed"] or 0
        pending = row["pending"] or 0
        cancelled = row["cancelled"] or 0
        expired = row["expired"] or 0

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

        # 仅有 draft_saved 才代表还有内容等待用户发布。
        if saved > 0:
            self.sm_round.transition(round_id, "awaiting_publish", "草稿已存，等待用户发布")
            report["final_status"] = "awaiting_publish"
            return

        if failed > 0:
            # 没有可发布草稿且至少一篇因执行失败 → 整轮失败（即便其它篇被取消/过期）。
            self.sm_round.transition(round_id, "failed", "没有剩余可发布草稿，且存在存草稿失败")
            report["final_status"] = "failed"
            return

        if cancelled + expired == total:
            # 所有草稿均被用户取消或超时；不能错误地进入 awaiting_publish。
            reason = "所有草稿均已取消" if expired == 0 else (
                "所有草稿均已过期" if cancelled == 0 else "所有草稿均已取消或过期"
            )
            self.sm_round.transition(round_id, "cancelled", reason)
            report["final_status"] = "cancelled"
            return

        # 仍存在不属于生产收尾范围的状态时 fail closed，避免进入无草稿可发的等待态。
        self.sm_round.transition(round_id, "failed", "未发现可发布草稿，且存在未预期的草稿状态组合")
        report["final_status"] = "failed"
