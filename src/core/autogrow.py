"""
自动闭环（自生长）— 复盘结论真正进入下一轮，并按条件自动开下一轮。

闭环：内容 → 发布 → 数据采集 → 复盘（SpiralEngine） → 【本模块】 → 下一轮内容

本模块补上三处断点：
1. 复盘结论 → 选题评分：StrategyFeedback.dimension_bias（按维度加/减分，有上限）
2. 复盘结论 → 文案生成：StrategyFeedback.copy_hints（每平台要点，进 LLM 提示词）
3. 自动开下一轮 + 目标达成自动收手：AutogrowController.tick()

安全边界（不因自动化放松）：
- 自动轮只生成草稿，发布仍需用户在平台确认（R9 人工闸门）
- 文案要点先过终极目标检查 check_strategy，命中红线的要点丢弃
- 草稿仍逐篇过 G2 合规检查（RoundExecutor 原逻辑不变）
- 有活跃轮次 / 目标全部达成 / 连续失败 / 每日上限 / 冷却期 / 全平台熔断 → 不开轮
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .timezone import business_now

logger = logging.getLogger(__name__)

ACTIVE_ROUND_STATUSES = ("created", "producing", "awaiting_publish", "collecting_metrics", "analyzing")
TERMINAL_OK = ("completed", "partial_completed")
TERMINAL_ALL = ("completed", "partial_completed", "failed", "cancelled")


# =====================================================================
# 复盘结论 → 可执行反馈
# =====================================================================

@dataclass
class StrategyFeedback:
    source_round_ids: list[int] = field(default_factory=list)
    dimension_bias: dict[str, float] = field(default_factory=dict)   # 维度 → 加减分（总分 0-100 制）
    bias_reasons: dict[str, list[str]] = field(default_factory=dict)
    copy_hints: dict[str, list[str]] = field(default_factory=dict)   # 平台 → 文案要点；"*" 为通用
    dropped_hints: list[str] = field(default_factory=list)           # 被合规闸门拦下的要点

    @property
    def empty(self) -> bool:
        return not self.dimension_bias and not any(self.copy_hints.values())

    def bias_for(self, dimension: Optional[str]) -> float:
        return float(self.dimension_bias.get(dimension or "", 0.0))

    def hints_for(self, platform: str) -> list[str]:
        return list(self.copy_hints.get("*", [])) + list(self.copy_hints.get(platform, []))

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _load_decision(raw) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def build_feedback(cfg, conn, guard=None) -> StrategyFeedback:
    """读取最近 N 轮已收口复盘 → 维度加减分 + 文案要点。没有复盘数据 → 空反馈（不猜）。"""
    ag = cfg.autogrow
    fb = StrategyFeedback()
    rows = conn.execute(
        "SELECT id, decision FROM spiral_rounds "
        f"WHERE status IN ({','.join('?' * len(TERMINAL_OK))}) AND decision IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (*TERMINAL_OK, ag.feedback_lookback_rounds),
    ).fetchall()
    if not rows:
        return fb

    max_bias = float(ag.feedback_max_bias)
    raw_bias: dict[str, float] = {}

    def add(dim: str, value: float, reason: str) -> None:
        if not dim:
            return
        raw_bias[dim] = raw_bias.get(dim, 0.0) + value
        fb.bias_reasons.setdefault(dim, []).append(reason)

    for idx, row in enumerate(rows):
        round_id = int(row[0])
        fb.source_round_ids.append(round_id)
        weight = ag.recency_decay ** idx

        # ① 维度表现：本轮维度均值 vs 本轮整体均值（R3：NULL 不计入）
        perf = conn.execute(
            "SELECT t.dimension, AVG(m.interaction_rate), COUNT(*) "
            "FROM metrics m JOIN contents c ON m.content_id = c.id "
            "JOIN topic_index t ON c.topic_id = t.id "
            "WHERE c.round_id = ? AND m.fetch_status IN ('success','partial') "
            "AND m.interaction_rate IS NOT NULL GROUP BY t.dimension",
            (round_id,),
        ).fetchall()
        total_n = sum(int(p[2]) for p in perf)
        if perf and total_n:
            overall = sum(float(p[1]) * int(p[2]) for p in perf) / total_n
            if overall > 0 and len(perf) >= 2:
                for dim, avg_ir, n in perf:
                    rel = max(-1.0, min(1.0, float(avg_ir) / overall - 1.0))
                    conf = min(1.0, int(n) / ag.full_confidence_samples)
                    delta = max_bias * rel * conf * weight
                    if abs(delta) >= ag.min_dimension_bias:
                        add(dim, delta, f"第{round_id}号轮互动率为整体的 {float(avg_ir) / overall:.0%}（样本 {n}）")

        decision = _load_decision(row[1])
        if idx != 0:
            continue  # 缺口 / 平台洞察只采用最近一轮（它们本身已是全库视角）

        # ② 缺口：覆盖缺口（表现好库存少）加分更多，验证缺口（样本不足）小幅加分试水
        for gap in decision.get("gaps") or []:
            dim = gap.get("dimension")
            if gap.get("gap_type") == "coverage_gap":
                add(dim, max_bias * ag.coverage_gap_bias_ratio, "覆盖缺口：表现好但库存少")
            elif gap.get("gap_type") == "validation_gap":
                add(dim, max_bias * ag.validation_gap_bias_ratio, "验证缺口：样本不足，试水")

        # ③ 平台洞察 → 文案要点
        for ins in decision.get("platform_insights") or []:
            platform = ins.get("platform")
            if not platform or not ins.get("sample_count"):
                continue
            hints = fb.copy_hints.setdefault(platform, [])
            bucket = (ins.get("best_title_style") or {}).get("best_bucket")
            if bucket:
                hints.append(f"该平台近期标题长度 {bucket} 字的互动率最高，标题长度尽量落在这个区间")
            tops = ins.get("top_dimensions") or []
            if tops and tops[0].get("dimension"):
                hints.append(f"该平台近期「{tops[0]['dimension']}」方向表现最好，可借鉴其切入角度")
            if ins.get("conclusion"):
                hints.append(str(ins["conclusion"])[:ag.max_copy_hint_chars])

        # ④ 策略调整描述 → 通用要点
        for adj in decision.get("strategy_adjustments") or []:
            if adj.get("description"):
                fb.copy_hints.setdefault("*", []).append(str(adj["description"])[:ag.max_copy_hint_chars])

    # ⑤ 最近几轮各平台互动率最高的标题，作为写法参考（不照抄）
    if fb.source_round_ids:
        marks = ",".join("?" * len(fb.source_round_ids))
        best = conn.execute(
            "SELECT c.platform, c.title, MAX(m.interaction_rate) AS ir "
            "FROM metrics m JOIN contents c ON m.content_id = c.id "
            f"WHERE c.round_id IN ({marks}) AND m.interaction_rate IS NOT NULL "
            "AND m.fetch_status IN ('success','partial') "
            "GROUP BY c.id ORDER BY ir DESC",
            fb.source_round_ids,
        ).fetchall()
        per_platform: dict[str, int] = {}
        for platform, title, _ir in best:
            if per_platform.get(platform, 0) >= ag.max_reference_titles_per_platform or not title:
                continue
            per_platform[platform] = per_platform.get(platform, 0) + 1
            fb.copy_hints.setdefault(platform, []).append(f"上一轮高互动标题参考写法（不要照抄）：「{title[:ag.max_copy_hint_chars]}」")

    fb.dimension_bias = {
        d: round(max(-max_bias, min(max_bias, v)), 2) for d, v in raw_bias.items() if abs(v) >= ag.min_dimension_bias
    }

    # 合规闸门：要点先过终极目标方向检查，命中红线直接丢弃（R2）
    if guard is not None:
        for key, hints in list(fb.copy_hints.items()):
            kept = []
            for h in hints:
                try:
                    ok = guard.check_strategy({"hint": h}).passes
                except Exception:  # noqa: BLE001 — 检查器异常不能默认通过
                    logger.exception("复盘要点合规检查异常，丢弃该要点")
                    ok = False
                (kept if ok else fb.dropped_hints).append(h)
            fb.copy_hints[key] = kept
    fb.copy_hints = {k: list(dict.fromkeys(v))[:ag.max_copy_hints_per_platform] for k, v in fb.copy_hints.items() if v}
    return fb


# =====================================================================
# 轮次创建（API 手动创建与自动开轮共用）
# =====================================================================

def create_round_record(
    cfg, conn, pvm, *, goal_id: Optional[str] = None, topic_count: int = 5,
    platforms: Optional[list[str]] = None, trigger_source: str = "manual",
    applied_feedback: Optional[str] = None,
) -> dict:
    row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM spiral_rounds").fetchone()
    round_number = (row[0] if row else 0) + 1
    if not goal_id:
        goal_row = conn.execute(
            "SELECT id FROM goals WHERE status='active' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        goal_id = goal_row[0] if goal_row else None
    cursor = conn.execute(
        "INSERT INTO spiral_rounds (goal_id, round_number, status, topic_ids, content_variants, "
        "trigger_source, applied_feedback) VALUES (?, ?, 'created', '[]', '[]', ?, ?)",
        (goal_id, round_number, trigger_source, applied_feedback),
    )
    conn.commit()
    # R18 / G-21：创建轮次即固化母计划签名
    plan, _sig, _at, source = pvm.load_plan()
    if source != "plan_settings":
        plan = pvm.build_plan(
            topic_count_per_platform=topic_count,
            platforms=platforms or cfg.platforms.enabled_keys(),
        )
    pvm.record(cursor.lastrowid, plan, author=f"round_create:{trigger_source}")
    return {"id": cursor.lastrowid, "round_number": round_number, "status": "created"}


# =====================================================================
# 自动开轮控制器
# =====================================================================

def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")[:26])
    except ValueError:
        return None


class AutogrowController:
    def __init__(self, cfg, conn, executor, *, goal_progress=None, notifier=None,
                 guard=None, pvm=None, enqueue_task=None):
        self.cfg = cfg
        self.conn = conn
        self.executor = executor
        self.goal_progress = goal_progress
        self.notifier = notifier
        self.guard = guard
        self.pvm = pvm
        self.enqueue_task = enqueue_task

    # ------------------------------------------------------------------
    def evaluate(self, force: bool = False) -> dict:
        """只判断不执行：返回 {can_start, action, reason, ...}，供状态接口与 tick 共用。"""
        ag = self.cfg.autogrow
        now = business_now(self.cfg)

        if not ag.enabled and not force:
            return {"can_start": False, "action": "skipped", "reason": "自动闭环已关闭"}

        active = self.conn.execute(
            "SELECT id, round_number, status FROM spiral_rounds "
            f"WHERE status IN ({','.join('?' * len(ACTIVE_ROUND_STATUSES))}) ORDER BY id DESC LIMIT 1",
            ACTIVE_ROUND_STATUSES,
        ).fetchone()
        if active:
            return {"can_start": False, "action": "skipped",
                    "reason": f"第 {active[1]} 轮仍在进行（{active[2]}），等它收口后再开下一轮"}

        # 自动闭环只接续用户跑通过的闭环：从没收口过一轮时，第一轮必须手动发起
        seeded = self.conn.execute(
            f"SELECT COUNT(*) FROM spiral_rounds WHERE status IN ({','.join('?' * len(TERMINAL_OK))})",
            TERMINAL_OK,
        ).fetchone()[0]
        if not seeded:
            return {"can_start": False, "action": "skipped",
                    "reason": "还没有完成过任何一轮：第一轮请手动发起，之后自动接续"}

        if ag.stop_when_goals_achieved:
            counts = self.conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) FROM goals"
            ).fetchone()
            total, active_goals, done = int(counts[0] or 0), int(counts[1] or 0), int(counts[2] or 0)
            if total and active_goals == 0 and done > 0:
                return {"can_start": False, "action": "stopped",
                        "reason": f"全部目标已达成（{done} 个），自动闭环停止；新建目标后自动恢复"}
            # 总目标 + 分平台模式：总目标达成即停止（平台子目标是手段，不是终点）
            if getattr(self.cfg.ultimate_goal, "goal_mode", "unified") == "hybrid":
                ns = self.conn.execute(
                    "SELECT SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) "
                    "FROM goals WHERE goal_level = 'north_star'"
                ).fetchone()
                if ns and int(ns[1] or 0) > 0 and int(ns[0] or 0) == 0:
                    return {"can_start": False, "action": "stopped",
                            "reason": "总目标已达成，自动闭环停止；新建总目标后自动恢复"}

        recent = self.conn.execute(
            f"SELECT status FROM spiral_rounds WHERE status IN ({','.join('?' * len(TERMINAL_ALL))}) "
            "ORDER BY id DESC LIMIT ?",
            (*TERMINAL_ALL, ag.max_consecutive_failures),
        ).fetchall()
        if len(recent) >= ag.max_consecutive_failures and all(r[0] == "failed" for r in recent):
            return {"can_start": False, "action": "stopped",
                    "reason": f"最近连续 {ag.max_consecutive_failures} 轮失败，自动闭环暂停；"
                              f"排查后手动跑通一轮即可恢复"}

        # 用户连续多轮都没有发布：继续开轮只会往草稿箱堆内容 → 暂停并说明
        unpublished_limit = max(2, int(ag.max_consecutive_failures))
        recent_ended = self.conn.execute(
            "SELECT r.id, (SELECT COUNT(*) FROM drafts d JOIN contents c ON d.content_id = c.id "
            "              WHERE c.round_id = r.id AND d.user_confirmed_at IS NOT NULL) AS pub "
            f"FROM spiral_rounds r WHERE r.status IN ({','.join('?' * len(TERMINAL_ALL))}) "
            "ORDER BY r.id DESC LIMIT ?",
            (*TERMINAL_ALL, unpublished_limit),
        ).fetchall()
        if len(recent_ended) >= unpublished_limit and all(int(r[1] or 0) == 0 for r in recent_ended):
            return {"can_start": False, "action": "stopped",
                    "reason": f"最近 {unpublished_limit} 轮的草稿都没有发布，自动闭环暂停，避免草稿箱堆积；"
                              f"发布任意一篇或手动开一轮即可恢复"}

        # 目标最多轮数（timeouts.goal_max_rounds）：防止目标定得不现实时无限循环
        max_rounds = int(getattr(self.cfg.timeouts, "goal_max_rounds", 0) or 0)
        if max_rounds:
            first_active = self.conn.execute(
                "SELECT MIN(created_at) FROM goals WHERE status = 'active'"
            ).fetchone()[0]
            if first_active:
                n = self.conn.execute(
                    "SELECT COUNT(*) FROM spiral_rounds WHERE created_at >= ? AND status != 'cancelled'",
                    (first_active,),
                ).fetchone()[0]
                if n >= max_rounds:
                    return {"can_start": False, "action": "stopped",
                            "reason": f"当前目标周期内已跑 {n} 轮（上限 {max_rounds}），仍未达成；"
                                      f"建议复盘目标是否合理，调整目标或上限后自动恢复"}


        plan, plan_at, plan_source = self._plan()
        cadence = (plan.get("cadence") or {}) if plan_source == "plan_settings" else {}
        if cadence.get("rounds_planned"):
            done = self.conn.execute(
                "SELECT COUNT(*) FROM spiral_rounds WHERE created_at >= ? AND status != 'cancelled'",
                (plan_at or "1970-01-01",),
            ).fetchone()[0]
            if done >= int(cadence["rounds_planned"]):
                return {"can_start": False, "action": "stopped",
                        "reason": f"母计划本阶段计划 {cadence['rounds_planned']} 轮已全部开出，"
                                  f"自动闭环停止；调整母计划节奏后自动恢复"}

        if not force:
            if ag.max_rounds_per_day == 0:
                return {"can_start": False, "action": "skipped", "reason": "每日自动轮上限为 0（已暂停）"}
            today = self.conn.execute(
                # R5：created_at 已是本地时间，不能再套 'localtime'（会二次换算跨日）
                "SELECT COUNT(*) FROM spiral_rounds WHERE trigger_source='auto' "
                "AND date(created_at) = date('now', 'localtime')"
            ).fetchone()[0]
            if today >= ag.max_rounds_per_day:
                return {"can_start": False, "action": "skipped",
                        "reason": f"今天已自动开 {today} 轮，达到每日上限 {ag.max_rounds_per_day}"}

            last = self.conn.execute(
                "SELECT round_number, created_at, transition_log FROM spiral_rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cooldown_hours = ag.cooldown_hours
            if cadence.get("round_interval_days"):
                cooldown_hours = int(float(cadence["round_interval_days"]) * 24)  # 母计划节奏优先
            if last and cooldown_hours:
                ended = None
                try:
                    log = json.loads(last[2]) if last[2] else []
                    ended = _parse_dt(log[-1].get("at")) if log else None
                except (TypeError, ValueError, AttributeError):
                    ended = None
                ended = ended or _parse_dt(last[1])
                if ended and now - ended < timedelta(hours=cooldown_hours):
                    ready = ended + timedelta(hours=cooldown_hours)
                    return {"can_start": False, "action": "skipped",
                            "reason": f"冷却中：第 {last[0]} 轮收口后需间隔 {cooldown_hours} 小时"
                                      + ("（母计划节奏）" if cadence.get("round_interval_days") else ""),
                            "next_eligible_at": ready.isoformat(timespec="minutes")}

        wanted = list(plan.get("enabled_platforms") or []) if plan_source == "plan_settings" else None
        platforms = self.executor._resolve_platforms(wanted) if self.executor else []
        usable = [p for p in platforms if not self.executor._platform_broken(p)]
        # 分平台 / 总目标+分平台：某平台自己的目标都已达成、且没有进行中的目标 → 这个平台不再自动开轮
        if getattr(self.cfg.ultimate_goal, "goal_mode", "unified") != "unified" and usable:
            done_only = set()
            for p in usable:
                c = self.conn.execute(
                    "SELECT SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) FROM goals "
                    "WHERE target_platform = ? AND COALESCE(goal_level,'platform') = 'platform'",
                    (p.value,),
                ).fetchone()
                if c and int(c[1] or 0) > 0 and int(c[0] or 0) == 0:
                    done_only.add(p)
            usable = [p for p in usable if p not in done_only]
        if not usable:
            return {"can_start": False, "action": "skipped", "reason": "没有可用平台（未启用或全部熔断）"}

        return {"can_start": True, "action": "start", "reason": "条件满足",
                "platforms": [p.value for p in usable], "plan_source": plan_source}

    def _plan(self) -> tuple[dict, Optional[str], str]:
        if self.pvm is None:
            return {}, None, "none"
        try:
            plan, _sig, at, source = self.pvm.load_plan()
            return plan or {}, at, source
        except Exception:  # noqa: BLE001
            logger.exception("autogrow 读取母计划失败")
            return {}, None, "none"

    # ------------------------------------------------------------------
    async def tick(self, force: bool = False) -> dict:
        """定时任务入口。force=True（用户点「立即执行」）只跳过冷却期与每日上限，安全条件不跳过。"""
        if self.goal_progress is not None:
            try:
                self.goal_progress.update_all()   # 先刷新目标进度，达成的目标会被置 completed
            except Exception:  # noqa: BLE001
                logger.exception("autogrow 刷新目标进度失败（继续判定）")

        verdict = self.evaluate(force=force)
        if not verdict["can_start"]:
            self._log(verdict["action"], verdict["reason"])
            if verdict["action"] == "stopped":
                self._notify_once("自动闭环已停止", verdict["reason"])
            logger.info("🌱 [autogrow] %s：%s", verdict["action"], verdict["reason"])
            return verdict

        ag = self.cfg.autogrow
        feedback = build_feedback(self.cfg, self.conn, self.guard) if ag.apply_feedback else StrategyFeedback()
        by_plan = verdict.get("plan_source") == "plan_settings"
        created = create_round_record(
            self.cfg, self.conn, self.pvm,
            topic_count=ag.topic_count, platforms=verdict["platforms"],
            trigger_source="auto", applied_feedback=feedback.to_json(),
        )
        round_id = created["id"]
        if self.enqueue_task is not None:
            task, created_task = self.enqueue_task(
                tenant_id=getattr(self, "tenant_id", None),
                round_id=round_id,
                topic_count=None if by_plan else ag.topic_count,
                platforms=verdict["platforms"],
                save_drafts=None,
                feedback=feedback.to_dict(),
            )
            reason = (
                f"第 {created['round_number']} 轮已进入全局任务队列"
                if created_task else f"第 {created['round_number']} 轮任务已在队列中"
            )
            self._log("queued", reason, round_id)
            return {
                "can_start": True, "action": "queued", "reason": reason,
                "round_id": round_id, "round_number": created["round_number"],
                "task_id": task.task_id, "feedback": feedback.to_dict(),
            }
        try:
            report = await self.executor.run(
                round_id, topic_count=None if by_plan else ag.topic_count,
                platform_values=verdict["platforms"], save_drafts=None, feedback=feedback,
            )
        except Exception as exc:  # noqa: BLE001 — 必须留痕，不能静默
            logger.exception("autogrow 第 %s 轮执行异常", created["round_number"])
            self._log("failed", f"第 {created['round_number']} 轮执行异常：{exc}", round_id)
            return {"can_start": True, "action": "failed", "reason": str(exc), "round_id": round_id}

        saved = report.get("drafts_saved", 0)
        status = report.get("final_status")
        reason = (f"第 {created['round_number']} 轮已自动开启：存草稿 {saved} 篇，状态 {status}；"
                  f"吃进复盘结论 {len(feedback.dimension_bias)} 个维度调整、"
                  f"{sum(len(v) for v in feedback.copy_hints.values())} 条文案要点")
        self._log("started" if status != "failed" else "failed", reason, round_id)
        if self.notifier is not None:
            self.notifier.emit(
                "round_completed",
                reason + ("，等你在平台确认发布" if saved else ""),
                title="自动开启下一轮",
            )
        return {"can_start": True, "action": "started", "reason": reason,
                "round_id": round_id, "round_number": created["round_number"],
                "report": report, "feedback": feedback.to_dict()}

    # ------------------------------------------------------------------
    def _log(self, action: str, reason: str, round_id: Optional[int] = None) -> None:
        try:
            self.conn.execute(
                "INSERT INTO autogrow_runs (ran_at, action, reason, round_id) VALUES (?, ?, ?, ?)",
                (business_now(self.cfg).isoformat(sep=" ", timespec="seconds"), action, reason[:500], round_id),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("autogrow_runs 写入失败")

    def _notify_once(self, title: str, reason: str) -> None:
        """停止类通知只在原因变化时发一次，避免每小时刷屏。"""
        if self.notifier is None:
            return
        prev = self.conn.execute(
            "SELECT reason FROM autogrow_runs WHERE action='stopped' ORDER BY id DESC LIMIT 1 OFFSET 1"
        ).fetchone()
        if prev and prev[0] == reason:
            return
        self.notifier.emit("round_completed", reason, title=title)

    def recent_runs(self, limit: int = 20) -> list[dict]:
        return [dict(zip(("id", "ran_at", "action", "reason", "round_id"), r)) for r in self.conn.execute(
            "SELECT id, ran_at, action, reason, round_id FROM autogrow_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()]
