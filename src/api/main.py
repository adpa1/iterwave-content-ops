"""
FastAPI 后端主应用 — 把所有模块串起来。

启动流程：
1. load_config() → Pydantic 校验通过
2. Database.initialize() → schema 迁移
3. get_adapters() → 根据 config.platforms.enabled_keys() 构造适配器
4. SchedulerEngine.start() → 7 类定时任务注册
5. uvicorn.run() → 监听端口

API 路由：
GET  /health              健康检查
GET  /api/status          系统概览（熔断器状态 / 运行中的轮次 / 今日运营配额）
GET  /api/rounds          轮次列表
POST /api/rounds          创建新轮次
GET  /api/rounds/{id}     轮次详情（含状态机可迁移检查）
POST /api/rounds/{id}/transition  轮次状态迁移
GET  /api/drafts          草稿列表
POST /api/drafts/{id}/publish_confirm  用户发布确认（R1 手动触发）
GET  /api/goals           目标列表
POST /api/goals           创建阶段目标
GET  /api/metrics         已采集数据列表
GET  /api/circuit_breaker 熔断器状态
POST /api/circuit_breaker/recover  触发 3 步恢复
POST /api/circuit_breaker/reset     手动重置
GET  /api/gaps            缺口分析
GET  /api/topics/score    选题评分（可传 topic_ids 范围）
GET  /api/hot_topics      热点雷达
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Callable, Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from src.core.config import load_config, get_config
from src.core.db import Database
from src.core.state_machine import RoundStateMachine, DraftStateMachine
from src.core.scoring.topic_eval import ScoringEngine, TopicItem
from src.core.guard.ultimate_goal_checker import UltimateGoalChecker
from src.core.metrics import Normalizer
from src.core.spiral import SpiralEngine
from src.core.scheduler import SchedulerEngine
from src.core.ops.circuit_breaker import CircuitBreakerEngine
from src.core.ops.rhythm import RhythmController
from src.core.ops.engine import OperationsEngine
from src.core.kb_reader import KbReader
from src.core.notify import Notifier, set_active as set_active_notifier
from src.core.hot_topics import HotTopicCollector
from src.core.recovery import RecoveryManager
from src.core.round_executor import RoundExecutor
from src.core.goal_progress import GoalProgressUpdater
from src.core.insights import PlatformInsightsEngine
from src.core.plan_version import PlanVersionManager
from src.core.ops.pause import pause_state, set_ops_paused
from src.core.config_store import (
    ConfigNotAllowedError, ConfigPatchError, apply_overrides, current_config_view,
    load_overrides, patch_config,
)
from src.adapters.base import Platform
from src.adapters import get_adapters

logger = logging.getLogger(__name__)

# =====================================================================
# 全局单例（启动时初始化一次）
# =====================================================================

_g_ctx: dict = {}

# G-22：正在执行的轮次集合。同一轮次并发触发会被拒绝（409），避免重复产出草稿
_running_rounds: set[int] = set()

# G-11：写接口幂等去重窗口（秒）。重复提交/连点在窗口内只落一条记录
_DEDUPE_TTL_SECONDS = 10.0
_dedupe_lock = threading.Lock()
_dedupe_cache: dict[str, tuple[float, dict]] = {}


def _dedupe_once(key: str, produce: Callable[[], dict]) -> tuple[dict, bool]:
    """同一 key 在 TTL 内只执行一次 produce，后续请求返回首次结果。

    连点 3 次只会插入 1 条记录，且 3 次响应返回同一个业务 id。
    """
    now = time.monotonic()
    with _dedupe_lock:
        hit = _dedupe_cache.get(key)
        if hit is not None and now - hit[0] < _DEDUPE_TTL_SECONDS:
            return hit[1], True
        for k, (ts, _) in list(_dedupe_cache.items()):
            if now - ts >= _DEDUPE_TTL_SECONDS:
                _dedupe_cache.pop(k, None)
        result = produce()
        _dedupe_cache[key] = (now, result)
        return result, False


def _build_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动阶段
        cfg = load_config()
        # G-13：先套用 Web 端持久化的覆盖值（data/config_overrides.json），
        # 再构造各模块 —— 保证 scheduler/adapter/ops 拿到的是同一份生效配置
        applied_overrides = apply_overrides(cfg, load_overrides())
        if applied_overrides:
            logger.info("⚙️ 已套用 %d 项运行时配置覆盖：%s",
                        len(applied_overrides), applied_overrides)
        db = Database(cfg)
        db.initialize()
        conn = db.connect()

        adapters = get_adapters(cfg, conn)
        cb = CircuitBreakerEngine(cfg, conn, adapters)
        rhythm = RhythmController(cfg)
        sm_round = RoundStateMachine(conn)
        sm_draft = DraftStateMachine(conn)
        scoring = ScoringEngine(cfg, conn)
        guard = UltimateGoalChecker(cfg)
        normalizer = Normalizer(conn)
        spiral = SpiralEngine(cfg, conn)
        scheduler = SchedulerEngine(cfg, cfg.resolve_path(cfg.paths.db_file))
        ops = OperationsEngine(cfg, conn, adapters, cb, rhythm)
        notifier = Notifier(cfg, conn)
        set_active_notifier(notifier)
        executor = RoundExecutor(
            cfg, conn, scoring, guard, adapters, sm_round, sm_draft,
            circuit_breaker=cb, notifier=notifier,
        )

        _g_ctx.update({
            "cfg": cfg, "db": db, "conn": conn,
            "adapters": adapters, "cb": cb, "rhythm": rhythm,
            "sm_round": sm_round, "sm_draft": sm_draft,
            "scoring": scoring, "guard": guard, "normalizer": normalizer,
            "spiral": spiral, "scheduler": scheduler, "ops": ops,
            "executor": executor, "notifier": notifier,
            "goal_progress": GoalProgressUpdater(cfg, conn),
            "insights": PlatformInsightsEngine(cfg, conn),
        })

        # 注入调度依赖并启动 APScheduler（8 类任务）
        kb_reader = KbReader(cfg)
        scheduler.wire(
            conn=conn, adapters=adapters, sm_draft=sm_draft, sm_round=sm_round,
            circuit_breaker=cb, notifier=notifier,
            ops=ops, kb_reader=kb_reader, spiral=spiral, scoring=scoring,
            goal_progress=_g_ctx["goal_progress"], guard=guard,
        )
        scheduler.start()
        # 断点恢复（R16/R17）：接续崩溃前未完成的任务，不重复写操作
        recovery_report = RecoveryManager(cfg, conn, scheduler).recover()
        _g_ctx["recovery"] = recovery_report

        # R18 母计划签名失效检查：启动时自动标记与当前计划不一致的活跃轮次
        pvm = PlanVersionManager(cfg, conn)
        invalidation = pvm.invalidate_changed_plans()
        if invalidation["stale_rounds"]:
            logger.warning(
                "🗑️ 母计划签名失效检查：%d 个活跃轮次已标记（签名 %s）",
                len(invalidation["stale_rounds"]),
                invalidation["current_signature"][:12],
            )
        _g_ctx["plan_version_manager"] = pvm

        logger.info("✅ FastAPI 应用启动完成，%d 个模块已加载", len(_g_ctx))

        try:
            yield
        finally:
            scheduler.shutdown()
            conn.close()
            logger.info("⏹️ FastAPI 应用关闭")

    app = FastAPI(
        title="Redbook Easel — 四平台图文运营 Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    _register_routes(app)

    # === 挂载 React 前端静态文件 (同 origin, 单进程) ===
    dist_dir = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    if dist_dir.is_dir():
        # assets 子目录（JS/CSS/图片）
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        # SPA fallback —— 任何非 API 的 GET 都回 index.html
        _API_PREFIXES = (
            "api/", "health", "docs", "openapi.json", "redoc", "favicon.ico",
        )

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            if full_path.startswith(_API_PREFIXES) or full_path == "":
                # 空路径 / API 路径都不该走到这里（API 路由已先匹配）
                if full_path == "":
                    return FileResponse(str(dist_dir / "index.html"))
                raise HTTPException(status_code=404)
            # 根目录静态文件（favicon.svg / robots.txt 等）优先返回真实文件，
            # 否则会被 SPA 兜底成 index.html（浏览器图标加载失败）
            candidate = (dist_dir / full_path).resolve()
            if candidate.is_file() and dist_dir.resolve() in candidate.parents:
                return FileResponse(str(candidate))
            return FileResponse(str(dist_dir / "index.html"))

        logger.info("📦 前端静态文件已挂载: %s", dist_dir)
    else:
        logger.warning("⚠️  web/dist 不存在，前端未挂载（请先 `cd web && npm run build`）")

    return app


# =====================================================================
# Pydantic 请求模型
# =====================================================================

class CreateGoalRequest(BaseModel):
    """R21：口径 5 字段必须齐备，缺一即 422（不接受默认值兜底）。"""

    goal_text: str
    target_metric: str
    target_value: float
    metric_scope: str = Field(description="delta=增量 / total=总量")
    baseline_value: float
    baseline_at: str
    metric_period_start: str
    metric_period_end: str
    data_source: str
    target_platform: Optional[str] = None

    @field_validator("goal_text", "target_metric", "baseline_at",
                     "metric_period_start", "metric_period_end", "data_source")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("R21 口径字段不能为空")
        return v

    @field_validator("metric_scope")
    @classmethod
    def _scope_enum(cls, v: str) -> str:
        if v not in ("delta", "total"):
            raise ValueError("metric_scope 只能是 delta 或 total")
        return v

    @field_validator("target_value")
    @classmethod
    def _target_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("target_value 必须大于 0")
        return v


class CreateRoundRequest(BaseModel):
    goal_id: Optional[str] = None
    topic_count: int = 5
    platforms: Optional[list[str]] = None


class TransitionRequest(BaseModel):
    target_status: str
    reason: str = ""


class RunRoundRequest(BaseModel):
    topic_count: int = 5
    platforms: Optional[list[str]] = None
    save_drafts: bool = True


class UpdatePlanRequest(BaseModel):
    """G-09 总计划（§19）：只允许改签名覆盖的 4 个字段 + 维度分布。"""

    topic_count_per_platform: int = Field(ge=1, le=50)
    dimension_allocation: dict[str, int] = Field(default_factory=dict)
    enabled_platforms: Optional[list[str]] = None
    content_format: str = "image_text"

    @field_validator("dimension_allocation")
    @classmethod
    def _alloc_non_negative(cls, v: dict[str, int]) -> dict[str, int]:
        for dim, n in v.items():
            if not str(dim).strip():
                raise ValueError("维度名不能为空")
            if int(n) < 0:
                raise ValueError(f"维度 {dim} 的条数不能为负")
        return {k: int(n) for k, n in v.items()}

    @field_validator("enabled_platforms")
    @classmethod
    def _valid_platforms(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        valid = {p.value for p in Platform}
        bad = [p for p in v if p not in valid]
        if bad:
            raise ValueError(f"未知平台: {bad}")
        if not v:
            raise ValueError("至少启用一个平台")
        return v


class UpdateContentRequest(BaseModel):
    """G-09 内容编辑：只允许改文案三件套，改后需重新过 G2。"""

    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[list[str]] = None
    reason: str = "UI 编辑"

    @field_validator("title", "body")
    @classmethod
    def _not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("标题/正文不能为空")
        return v


class RejectContentRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("驳回理由不能为空")
        return v


def _parse_schedule_time(raw: str) -> str:
    """把 YYYY-MM-DD / YYYY-MM-DDTHH:MM[:SS] / YYYY-MM-DD HH:MM 归一为本地时区 ISO 串。

    强制本地时间（R5）：不接受以 Z 结尾的 UTC 串，避免排期整体偏移 8 小时。
    """
    text = raw.strip().replace(" ", "T")
    if text.endswith("Z"):
        raise HTTPException(status_code=422, detail="排期时间必须是本地时间（Asia/Shanghai），不接受 UTC Z 串")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"排期时间格式无法解析：{raw}")
    if dt.tzinfo is not None:
        raise HTTPException(status_code=422, detail="排期时间不要带时区后缀，按本地时间解释")
    return dt.isoformat(timespec="minutes")


def _write_schedule(conn, draft_id: str, scheduled: Optional[str]) -> dict:
    """写 drafts.scheduled_publish_at。只允许 draft_pending / draft_saved 改排期。"""
    row = conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"草稿不存在：{draft_id}")
    status = dict(row)["status"]
    if status not in ("draft_pending", "draft_saved"):
        raise HTTPException(
            status_code=409,
            detail=f"草稿状态 {status} 不允许改排期（仅 draft_pending / draft_saved）",
        )
    value = _parse_schedule_time(scheduled) if scheduled else None
    conn.execute(
        "UPDATE drafts SET scheduled_publish_at = ? WHERE id = ?", (value, draft_id)
    )
    conn.commit()
    return {"draft_id": draft_id, "scheduled_publish_at": value, "status": status}


def _unscheduled_drafts(conn) -> list[dict]:
    """未排期草稿（供 UI 直接挑选排期）。scheduled_publish_at IS NULL，不是空串。"""
    rows = conn.execute(
        "SELECT d.id, d.platform, d.status, c.title, c.round_id "
        "FROM drafts d JOIN contents c ON d.content_id = c.id "
        "WHERE d.scheduled_publish_at IS NULL AND d.status IN ('draft_pending','draft_saved') "
        "ORDER BY d.created_at DESC LIMIT 50"
    ).fetchall()
    return [dict(r) for r in rows]


class ScheduleWriteRequest(BaseModel):
    draft_id: str
    scheduled_publish_at: str

    @field_validator("draft_id", "scheduled_publish_at")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("字段不能为空")
        return v


class SchedulePatchRequest(BaseModel):
    """scheduled_publish_at=None → 取消排期（写回 NULL，不是写空串）。"""

    scheduled_publish_at: Optional[str] = None


class ConfigPatchRequest(BaseModel):
    """G-13 Settings / G-24 热点门限：白名单字段批量修改。"""

    updates: dict[str, object]

    @field_validator("updates")
    @classmethod
    def _non_empty(cls, v: dict) -> dict:
        if not v:
            raise ValueError("updates 不能为空")
        return v


class CreateAssetRequest(BaseModel):
    """G-13 内容资产：外部参考 / 个人观点 / 内容模板 三类真实录入。"""

    asset_type: str
    title: str
    body: Optional[str] = None
    source_url: Optional[str] = None
    platform: Optional[str] = None

    @field_validator("asset_type")
    @classmethod
    def _type_enum(cls, v: str) -> str:
        if v not in ("reference", "voice", "template"):
            raise ValueError("asset_type 只能是 reference / voice / template")
        return v

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("标题不能为空")
        return v.strip()

    @field_validator("platform")
    @classmethod
    def _platform_enum(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if v not in {p.value for p in Platform}:
            raise ValueError(f"未知平台：{v}")
        return v


# =====================================================================
# 路由注册
# =====================================================================

def _register_routes(app: FastAPI) -> None:
    g = _g_ctx  # 占位，实际在 lifespan 内注入
    # 用闭包避免启动前 _g_ctx 为空
    def _g():
        return _g_ctx

    # --- 健康检查 ---
    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0", "timestamp": datetime.now().isoformat()}

    # --- 系统概览 ---
    @app.get("/api/status")
    def system_status():
        g2 = _g()
        conn = g2["conn"]

        # 熔断器状态
        cb_events = g2["cb"].active_events()
        broken_platforms = sorted({e["platform"] for e in cb_events})

        # 运行中轮次
        row = conn.execute(
            "SELECT COUNT(*) FROM spiral_rounds WHERE status IN ('producing','awaiting_publish','collecting_metrics')"
        ).fetchone()
        active_rounds = row[0]

        # 已发布样本数
        row = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE status = 'published'"
        ).fetchone()
        published_count = row[0]

        cfg = g2["cfg"]
        # R20/B-1：登录态是「能否真实操作」的前置条件，前端据此打 🔴 标注
        from src.core.login_manager import has_login_state

        logged_in = [
            p.value for p in Platform
            if p.value in cfg.platforms.enabled_keys() and has_login_state(p)
        ]
        return {
            "enabled_platforms": cfg.platforms.enabled_keys(),
            "logged_in_platforms": logged_in,
            "platforms_without_login": [
                p for p in cfg.platforms.enabled_keys() if p not in logged_in
            ],
            "circuit_broken_platforms": broken_platforms,
            "active_rounds": active_rounds,
            "published_samples": published_count,
            "scoring_mode": g2["scoring"]._determine_mode(),
            "recovery": g2.get("recovery"),
        }

    # --- 目标 ---
    @app.get("/api/goals")
    def list_goals():
        conn = _g()["conn"]
        rows = conn.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            item = dict(r)
            # R21：达成率后端算，任一分量 NULL → NULL（前端显示灰色「数据待采集」）
            item["achievement_rate"] = GoalProgressUpdater.achievement_rate(
                current=item.get("current_value"),
                baseline=item.get("baseline_value"),
                target=item.get("target_value"),
            )
            result.append(item)
        return result

    @app.post("/api/goals")
    def create_goal(req: CreateGoalRequest):
        conn = _g()["conn"]
        key = "goal:" + hashlib.sha256(req.model_dump_json().encode()).hexdigest()

        def _produce() -> dict:  # G-11：10s 内相同请求只落一条
            goal_id = f"goal-{uuid.uuid4().hex[:10]}"
            conn.execute(
                "INSERT INTO goals (id, goal_text, target_metric, target_value, "
                "target_platform, metric_scope, data_source, baseline_value, baseline_at, "
                "metric_period_start, metric_period_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (goal_id, req.goal_text, req.target_metric, req.target_value,
                 req.target_platform, req.metric_scope, req.data_source,
                 req.baseline_value, req.baseline_at,
                 req.metric_period_start, req.metric_period_end),
            )
            conn.commit()
            return {"id": goal_id, "message": "目标已创建"}

        result, deduped = _dedupe_once(key, _produce)
        return {**result, "deduped": deduped}

    @app.post("/api/goals/progress/refresh")
    def refresh_goal_progress():
        """按 target_metric 重新聚合 metrics → 回写 current_value（R21）。"""
        updater = GoalProgressUpdater(_g()["cfg"], _g()["conn"])
        values = updater.update_all()
        conn = _g()["conn"]
        rows = conn.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["achievement_rate"] = GoalProgressUpdater.achievement_rate(
                current=item.get("current_value"),
                baseline=item.get("baseline_value"),
                target=item.get("target_value"),
            )
            result.append(item)
        return {"updated": values, "goals": result}

    # --- 轮次 ---
    @app.get("/api/rounds")
    def list_rounds(status: Optional[str] = Query(None)):
        conn = _g()["conn"]
        sql = "SELECT * FROM spiral_rounds"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT 50"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/rounds")
    def create_round(req: CreateRoundRequest):
        conn = _g()["conn"]
        key = "round:" + hashlib.sha256(req.model_dump_json().encode()).hexdigest()

        def _produce() -> dict:  # G-11：10s 内相同请求只落一条
            row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM spiral_rounds").fetchone()
            round_number = row[0] + 1 if row else 1

            # 查 goal
            goal_id = req.goal_id
            if not goal_id:
                goal_row = conn.execute(
                    "SELECT id FROM goals WHERE status='active' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                goal_id = goal_row[0] if goal_row else None

            cursor = conn.execute(
                "INSERT INTO spiral_rounds (goal_id, round_number, status, topic_ids, content_variants) "
                "VALUES (?, ?, 'created', ?, ?)",
                (goal_id, round_number, "[]", "[]"),
            )
            conn.commit()

            # R18 / G-21：创建轮次即固化母计划签名（写 rounds.plan_signature +
            # signature_history），否则本轮在「改计划 → 下游失效」时无从比对
            pvm = _g()["plan_version_manager"]
            plan, _sig, _at, source = pvm.load_plan()
            if source != "plan_settings":
                plan = pvm.build_plan(
                    topic_count_per_platform=req.topic_count,
                    platforms=req.platforms or _g()["cfg"].platforms.enabled_keys(),
                )
            pvm.record(cursor.lastrowid, plan, author="round_create")

            return {"id": cursor.lastrowid, "round_number": round_number, "status": "created"}

        result, deduped = _dedupe_once(key, _produce)
        return {**result, "deduped": deduped}

    @app.get("/api/rounds/{round_id}")
    def get_round(round_id: int):
        conn = _g()["conn"]
        row = conn.execute("SELECT * FROM spiral_rounds WHERE id = ?", (round_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"轮次 {round_id} 不存在")
        # 附带 drafts 列表
        drafts = conn.execute(
            "SELECT * FROM drafts WHERE content_id IN (SELECT id FROM contents WHERE round_id = ?)",
            (round_id,),
        ).fetchall()
        result = dict(row)
        result["drafts"] = [dict(d) for d in drafts]
        return result

    @app.post("/api/rounds/{round_id}/run")
    async def run_round(round_id: int, req: RunRoundRequest):
        """执行一轮：选题 → 文案 → G2 检查 → 卡片 → 存草稿（P0-1 主流程）。

        G-22：同一轮次运行中重复调用返回 409，不重复产出草稿。
        """
        if round_id in _running_rounds:
            raise HTTPException(
                409, f"轮次 {round_id} 正在执行中，请等待本次执行结束（禁止并发重复触发）"
            )
        _running_rounds.add(round_id)
        try:
            report = await _g()["executor"].run(
                round_id,
                topic_count=req.topic_count,
                platform_values=req.platforms,
                save_drafts=req.save_drafts,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            _running_rounds.discard(round_id)
        return report

    @app.post("/api/rounds/{round_id}/transition")
    def round_transition(round_id: int, req: TransitionRequest):
        ok, msg = _g()["sm_round"].can_transition(round_id, req.target_status)
        if not ok:
            raise HTTPException(400, msg)
        _g()["sm_round"].transition(round_id, req.target_status, reason=req.reason)
        return {"round_id": round_id, "new_status": req.target_status}

    # --- 草稿 ---
    @app.get("/api/drafts")
    def list_drafts(
        status: Optional[str] = Query(None),
        platform: Optional[str] = Query(None),
    ):
        conn = _g()["conn"]
        sql = "SELECT d.*, c.title, c.platform as cfg_platform FROM drafts d "
        sql += "LEFT JOIN contents c ON d.content_id = c.id"
        where: list[str] = []
        params: list = []
        if status:
            where.append("d.status = ?")
            params.append(status)
        if platform:
            where.append("d.platform = ?")
            params.append(platform)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY d.created_at DESC LIMIT 100"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/drafts/{draft_id}/publish_confirm")
    def confirm_publish(draft_id: str):
        """R1 用户手动点击"✅ 我已发布"触发 user_published。"""
        conn = _g()["conn"]
        row = conn.execute(
            "SELECT platform FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        ok = _g()["sm_draft"].transition(draft_id, "user_published", reason="用户确认发布")
        if not ok:
            raise HTTPException(400, "状态迁移失败")
        # 首篇 user_published → collecting_metrics
        round_id = _find_round_for_draft(conn, draft_id)
        if round_id:
            _g()["sm_round"].on_first_publish_confirmed(round_id)
        # G-15 场景：发布确认留痕（用户手动确认，非系统自动发布）
        _g()["notifier"].emit(
            "publish_confirmed",
            f"草稿 {draft_id} 已由用户确认发布，进入数据采集",
            platform=dict(row)["platform"] if row else None,
        )
        return {"draft_id": draft_id, "status": "user_published"}

    @app.post("/api/drafts/{draft_id}/cancel")
    def cancel_draft(draft_id: str):
        """用户点"❌ 不发了" → 草稿转 cancelled（R9：collecting_metrics 期间仍可取消未发布草稿）。"""
        conn = _g()["conn"]
        row = conn.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            raise HTTPException(404, "草稿不存在")
        ok = _g()["sm_draft"].transition(draft_id, "cancelled", reason="用户取消发布")
        if not ok:
            raise HTTPException(400, f"状态迁移失败（当前 {row['status']}）")
        return {"draft_id": draft_id, "status": "cancelled"}

    # --- G-09 母计划（Production 总计划 tab，§19 / R18）---
    @app.get("/api/plans")
    def get_plan():
        pvm = _g()["plan_version_manager"]
        plan, signature, updated_at, source = pvm.load_plan()
        cfg = _g()["cfg"]
        conn = _g()["conn"]

        enabled = set(plan.get("enabled_platforms") or [])
        per_platform = {}
        for p in Platform:
            per_platform[p.value] = {
                "enabled": p.value in enabled,
                "config_enabled": bool(getattr(cfg.platforms, p.value).enabled),
                "topic_count": plan.get("topic_count_per_platform"),
            }

        history = [dict(r) for r in conn.execute(
            "SELECT plan_signature, plan_snapshot, round_id, created_at, author "
            "FROM signature_history ORDER BY id DESC LIMIT 10"
        ).fetchall()]
        for h in history:
            try:
                h["plan_snapshot"] = json.loads(h["plan_snapshot"])
            except (TypeError, ValueError):
                pass

        # R18 可视化：签名与当前母计划不一致的活跃轮次
        active = conn.execute(
            "SELECT id, round_number, status, plan_signature FROM spiral_rounds "
            "WHERE status IN ('created','producing','awaiting_publish') "
            "AND plan_signature IS NOT NULL"
        ).fetchall()
        stale = [dict(r) for r in active if r["plan_signature"] != signature]

        return {
            "plan": plan,
            "signature": signature,
            "updated_at": updated_at,
            "source": source,
            "per_platform": per_platform,
            "history": history,
            "stale_rounds": stale,
        }

    @app.post("/api/plans")
    def update_plan(req: UpdatePlanRequest):
        """保存母计划 → 新签名 + signature_history + 下游失效（R18）。"""
        pvm = _g()["plan_version_manager"]
        cfg = _g()["cfg"]
        _, previous_signature, _, _ = pvm.load_plan()

        platforms = req.enabled_platforms or cfg.platforms.enabled_keys()
        plan = pvm.build_plan(
            topic_count_per_platform=req.topic_count_per_platform,
            platforms=platforms,
            dimension_allocation=req.dimension_allocation,
            content_format=req.content_format,
        )
        result = pvm.save_plan(plan, author="web-ui")
        result["previous_signature"] = previous_signature
        result["changed"] = previous_signature != result["signature"]
        return result

    @app.post("/api/plan_adjustments/adopt")
    def adopt_plan_adjustments(req: dict):
        """复盘「采纳建议」→ 把缺口维度写回母计划（R18：签名变化自动失效下游）。"""
        raw = (req or {}).get("dimensions") or []
        dims = list(dict.fromkeys(str(d).strip() for d in raw if str(d).strip()))
        if not dims:
            raise HTTPException(400, "dimensions 不能为空")
        pvm = _g()["plan_version_manager"]
        cfg = _g()["cfg"]
        plan, previous_signature, _, _ = pvm.load_plan()

        allocation = dict(plan.get("dimension_allocation") or {})
        for d in dims:
            allocation[d] = int(allocation.get(d) or 0) + 1

        new_plan = pvm.build_plan(
            topic_count_per_platform=plan.get("topic_count_per_platform") or 5,
            platforms=plan.get("enabled_platforms") or cfg.platforms.enabled_keys(),
            dimension_allocation=allocation,
            content_format=plan.get("content_format") or "image_text",
        )
        result = pvm.save_plan(new_plan, author="web-ui:adopt-gaps")
        result.update({
            "previous_signature": previous_signature,
            "adopted_dimensions": dims,
            "dimension_allocation": allocation,
        })
        return result

    # --- G-09 内容列表 / 编辑 / 重检 / 驳回 ---
    def _content_row(conn, content_id: str):
        row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"内容 {content_id} 不存在")
        return row

    @app.get("/api/contents")
    def list_contents(
        round_id: int = Query(..., description="轮次 ID"),
        platform: Optional[str] = Query(None, pattern="^(xhs|douyin|kuaishou|wechat_channels)$"),
    ):
        conn = _g()["conn"]
        sql = (
            "SELECT c.*, d.id AS draft_id, d.status AS draft_status, "
            "       d.error_msg AS draft_error, d.saved_at AS draft_saved_at "
            "FROM contents c LEFT JOIN drafts d ON d.content_id = c.id "
            "WHERE c.round_id = ?"
        )
        params: list = [round_id]
        if platform:
            sql += " AND c.platform = ?"
            params.append(platform)
        sql += " ORDER BY c.platform, c.created_at"

        items: list[dict] = []
        for r in conn.execute(sql, params).fetchall():
            row = dict(r)
            try:
                row["tags"] = json.loads(row["tags"]) if row["tags"] else []
            except (TypeError, ValueError):
                row["tags"] = []
            try:
                paths = json.loads(row["image_paths"]) if row["image_paths"] else []
            except (TypeError, ValueError):
                paths = []
            row["image_count"] = len(paths)
            items.append(row)
        return {"round_id": round_id, "items": items}

    @app.patch("/api/contents/{content_id}")
    def update_content(content_id: str, req: UpdateContentRequest):
        """人工编辑文案 → 写 user_override 留痕（§16.3），不自动放行 G2。"""
        conn = _g()["conn"]
        row = _content_row(conn, content_id)

        title = req.title if req.title is not None else row["title"]
        body = req.body if req.body is not None else row["body"]
        tags_json = (
            json.dumps(req.tags, ensure_ascii=False)
            if req.tags is not None else row["tags"]
        )
        changed = (
            title != row["title"] or body != row["body"] or tags_json != row["tags"]
        )

        conn.execute(
            "UPDATE contents SET title = ?, body = ?, tags = ?, word_count = ? WHERE id = ?",
            (title, body, tags_json, len(body or ""), content_id),
        )
        if changed:
            conn.execute(
                "INSERT INTO user_override (round_id, override_field, original_value, "
                "override_value, reason, overridden_by, overridden_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["round_id"], "content",
                    json.dumps({"title": row["title"], "body_len": len(row["body"] or "")},
                               ensure_ascii=False)[:500],
                    json.dumps({"title": title, "body_len": len(body or "")},
                               ensure_ascii=False)[:500],
                    req.reason or "UI 编辑", "web-ui",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        conn.commit()
        return {
            "id": content_id, "changed": changed, "title": title, "body": body,
            "tags": json.loads(tags_json) if tags_json else [],
        }

    @app.post("/api/contents/{content_id}/recheck")
    def recheck_content(content_id: str):
        """对编辑后的文案重新跑 G2（R2：检查器异常/无法解析 → 不能默认通过）。"""
        conn = _g()["conn"]
        guard = _g()["guard"]
        row = _content_row(conn, content_id)
        try:
            tags = json.loads(row["tags"]) if row["tags"] else []
        except (TypeError, ValueError):
            tags = []

        try:
            outcome = guard.check_content(row["title"], row["body"], tags)
        except Exception as exc:  # noqa: BLE001 — R2 红线
            logger.exception("G2 重检异常 content=%s", content_id)
            raise HTTPException(500, f"检查器异常，不能默认通过：{exc}") from exc

        conn.execute(
            "UPDATE contents SET guard_outcome = ?, guard_reason = ? WHERE id = ?",
            (outcome.result.value, outcome.reason, content_id),
        )
        conn.commit()
        return {
            "id": content_id,
            "result": outcome.result.value,
            "passes": outcome.passes,
            "reason": outcome.reason,
            "redline_hits": outcome.redline_hits or [],
        }

    @app.post("/api/contents/{content_id}/reject")
    def reject_content(content_id: str, req: RejectContentRequest):
        """人工驳回：把理由写到该内容对应草稿的 error_msg，并留痕 user_override。"""
        conn = _g()["conn"]
        row = _content_row(conn, content_id)
        message = f"[已驳回] {req.reason.strip()}"

        cur = conn.execute(
            "UPDATE drafts SET error_msg = ? "
            "WHERE content_id = ? AND status IN ('draft_pending','draft_saved')",
            (message, content_id),
        )
        conn.execute(
            "INSERT INTO user_override (round_id, override_field, original_value, "
            "override_value, reason, overridden_by, overridden_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["round_id"], "review_reject",
                json.dumps({"title": row["title"]}, ensure_ascii=False)[:500],
                "rejected", req.reason.strip(), "web-ui",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        return {
            "id": content_id,
            "affected_drafts": cur.rowcount,
            "message": message,
        }

    # --- G-09 生产线阶段计数（G → G2 → H → I）---
    @app.get("/api/rounds/{round_id}/pipeline")
    def round_pipeline(round_id: int):
        conn = _g()["conn"]
        row = conn.execute("SELECT * FROM spiral_rounds WHERE id = ?", (round_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"轮次 {round_id} 不存在")

        try:
            topics = json.loads(row["topic_ids"]) if row["topic_ids"] else []
        except (TypeError, ValueError):
            topics = []

        one = lambda sql, p=(round_id,): conn.execute(sql, p).fetchone()[0]  # noqa: E731
        contents_total = one("SELECT COUNT(*) FROM contents WHERE round_id = ?")
        guard_pass = one(
            "SELECT COUNT(*) FROM contents WHERE round_id = ? "
            "AND guard_outcome IN ('pass','warn')"
        )
        cards = one(
            "SELECT COUNT(*) FROM contents WHERE round_id = ? "
            "AND image_paths IS NOT NULL AND image_paths NOT IN ('', '[]')"
        )
        guard_rejected = row["guard_rejections"] if "guard_rejections" in row.keys() else None

        draft_rows = conn.execute(
            "SELECT d.status AS status, COUNT(*) AS n FROM drafts d "
            "JOIN contents c ON d.content_id = c.id WHERE c.round_id = ? GROUP BY d.status",
            (round_id,),
        ).fetchall()
        drafts_by_status = {r["status"]: r["n"] for r in draft_rows}

        stages = [
            {"key": "G", "label": "G 选题入选", "count": len(topics),
             "note": "本轮实际参与生产的选题数"},
            {"key": "G1", "label": "G1 文案生成", "count": contents_total,
             "note": "通过 G2 后落库的文案数（同一选题 × 平台一篇）"},
            {"key": "G2", "label": "G2 检查通过", "count": guard_pass,
             "note": f"拦截（含重生成前）{guard_rejected if guard_rejected is not None else '—'} 次"},
            {"key": "H", "label": "H 卡片制作", "count": cards,
             "note": "image_paths 非空的内容数"},
            {"key": "I", "label": "I 存草稿成功",
             "count": drafts_by_status.get("draft_saved", 0),
             "note": "R1：只存草稿，永不自动发布"},
        ]

        per_platform: list[dict] = []
        rows_p = conn.execute(
            "SELECT c.platform AS platform, COUNT(*) AS contents, "
            "SUM(CASE WHEN c.guard_outcome IN ('pass','warn') THEN 1 ELSE 0 END) AS guard_pass, "
            "SUM(CASE WHEN c.image_paths IS NOT NULL AND c.image_paths NOT IN ('', '[]') "
            "         THEN 1 ELSE 0 END) AS cards "
            "FROM contents c WHERE c.round_id = ? GROUP BY c.platform",
            (round_id,),
        ).fetchall()
        for r in rows_p:
            d = conn.execute(
                "SELECT SUM(CASE WHEN d.status = 'draft_saved' THEN 1 ELSE 0 END) AS saved, "
                "SUM(CASE WHEN d.status = 'draft_pending' THEN 1 ELSE 0 END) AS pending, "
                "SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) AS failed "
                "FROM drafts d JOIN contents c ON d.content_id = c.id "
                "WHERE c.round_id = ? AND c.platform = ?",
                (round_id, r["platform"]),
            ).fetchone()
            per_platform.append({
                "platform": r["platform"],
                "contents": r["contents"],
                "guard_pass": r["guard_pass"],
                "cards": r["cards"],
                "draft_saved": d["saved"] or 0,
                "draft_pending": d["pending"] or 0,
                "draft_failed": d["failed"] or 0,
            })

        return {
            "round_id": round_id,
            "round_number": row["round_number"],
            "status": row["status"],
            "stages": stages,
            "guard_rejections": guard_rejected,
            "drafts_by_status": drafts_by_status,
            "per_platform": per_platform,
        }


    # --- 内容日历排期（§24.3）---
    @app.get("/api/schedule")
    def list_schedule(
        start_date: Optional[str] = Query(None, description="YYYY-MM-DD 起始日期"),
        end_date: Optional[str] = Query(None, description="YYYY-MM-DD 结束日期"),
    ):
        """按 scheduled_publish_at 返回排期事件（无排期时用 saved_at 兜底）。"""
        from datetime import datetime, timedelta
        conn = _g()["conn"]

        if start_date is None:
            start_date = datetime.now().strftime("%Y-%m-%d")
        if end_date is None:
            end_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

        sql = (
            "SELECT d.id, d.platform, d.status, d.scheduled_publish_at, d.saved_at, "
            "       c.title, c.round_id "
            "FROM drafts d JOIN contents c ON d.content_id = c.id "
            "WHERE (d.scheduled_publish_at IS NOT NULL "
            "        AND DATE(d.scheduled_publish_at) BETWEEN ? AND ?) "
            "   OR (d.scheduled_publish_at IS NULL "
            "        AND d.saved_at IS NOT NULL "
            "        AND DATE(d.saved_at) BETWEEN ? AND ?) "
        )
        params = [start_date, end_date, start_date, end_date]
        sql += " ORDER BY COALESCE(d.scheduled_publish_at, d.saved_at) ASC LIMIT 200"

        rows = conn.execute(sql, params).fetchall()
        items: list[dict] = []
        for r in rows:
            row = dict(r)
            time_field = row["scheduled_publish_at"] or row["saved_at"]
            day = None
            if time_field:
                try:
                    day = datetime.fromisoformat(str(time_field)).day
                except ValueError:
                    pass
            items.append({
                "id": row["id"],
                "title": row["title"],
                "platform": row["platform"],
                "status": row["status"],
                "round_id": row["round_id"],
                "scheduled": row["scheduled_publish_at"],
                "saved_at": row["saved_at"],
                "day": day,
            })
        return {"start_date": start_date, "end_date": end_date, "items": items,
                "unscheduled": _unscheduled_drafts(conn)}

    @app.post("/api/schedule")
    def create_schedule(req: "ScheduleWriteRequest"):
        """给单篇草稿写排期（§24.3）。只写 drafts.scheduled_publish_at，不触发任何发布动作（R1）。"""
        conn = _g()["conn"]
        key = f"schedule:{req.draft_id}:{req.scheduled_publish_at}"
        result, deduped = _dedupe_once(key, lambda: _write_schedule(conn, req.draft_id,
                                                                  req.scheduled_publish_at))
        return {**result, "deduped": deduped}

    @app.patch("/api/schedule/{draft_id}")
    def update_schedule(draft_id: str, req: "SchedulePatchRequest"):
        """改排期；scheduled_publish_at 传 null = 取消排期。"""
        conn = _g()["conn"]
        return _write_schedule(conn, draft_id, req.scheduled_publish_at)

    # --- 熔断器 ---
    @app.get("/api/circuit_breaker")
    def circuit_breaker_status():
        cb = _g()["cb"]
        events = cb.active_events()
        for e in events:
            e["is_broken"] = cb.is_circuit_broken(Platform(e["platform"]))
        return {"events": events, "broken_platforms": sorted({e["platform"] for e in events})}

    @app.post("/api/circuit_breaker/recover")
    async def circuit_breaker_recover(platform: str):
        try:
            p = Platform(platform)
        except ValueError:
            raise HTTPException(400, f"未知平台: {platform}")
        result = await _g()["cb"].try_recover(p)
        if not result["passed"]:
            raise HTTPException(400, result["reason"])
        return result

    @app.post("/api/circuit_breaker/reset")
    def circuit_breaker_reset(platform: str):
        try:
            p = Platform(platform)
        except ValueError:
            raise HTTPException(400, f"未知平台: {platform}")
        count = _g()["cb"].manual_reset(p)
        return {"reset_count": count}

    # --- 缺口分析 ---
    @app.get("/api/gaps")
    def gap_analysis(round_id: Optional[int] = Query(None)):
        spiral = _g()["spiral"]
        insights = _g()["insights"]
        if round_id:
            result = spiral.analyze_round(round_id).__dict__
        else:
            # 最新一轮
            conn = _g()["conn"]
            row = conn.execute("SELECT id FROM spiral_rounds ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:
                # 空库：必须返回与 SpiralAnalysis 一致的字段形状（前端按
                # strategy_adjustments/evidence 渲染，缺键会直接白屏）
                return {
                    "round_id": None,
                    "gaps": [],
                    "strategy_adjustments": [],
                    "ops_adjustments": [],
                    "evidence": {},
                    "platform_breakdown": [],
                    "message": "暂无轮次数据",
                }
            result = spiral.analyze_round(row[0]).__dict__
        # G-04 单平台纵深分析（第四通道）：写入 platform_insights 并按平台返回拆解
        result["platform_breakdown"] = insights.ensure_fresh(round_id=round_id)
        return result

    @app.get("/api/insights")
    def platform_insights(
        platform: Optional[str] = Query(None, pattern="^(xhs|douyin|kuaishou|wechat_channels)$"),
        days: int = Query(90, ge=1, le=365),
    ):
        """单平台纵深分析：传 platform 就只重算该平台，否则读缓存/全量重算。"""
        engine = _g()["insights"]
        if platform:
            return {"items": [i.to_dict() for i in engine.analyze(days=days, platforms=[platform])]}
        return {"items": engine.ensure_fresh(days=days)}

    # --- 已采集数据列表（Analytics 页数据源）---
    @app.get("/api/metrics")
    def list_metrics(
        platform: Optional[str] = Query(None, pattern="^(xhs|douyin|kuaishou|wechat_channels)$"),
        fetch_status: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ):
        """已采集数据列表（R3：NULL 原样返回，不补 0）。"""
        conn = _g()["conn"]
        sql = (
            "SELECT m.*, c.title AS content_title, c.round_id "
            "FROM metrics m LEFT JOIN contents c ON m.content_id = c.id "
            "WHERE 1=1"
        )
        params: list = []
        if platform:
            sql += " AND m.platform = ?"
            params.append(platform)
        if fetch_status:
            sql += " AND m.fetch_status = ?"
            params.append(fetch_status)
        sql += " ORDER BY m.fetched_at DESC NULLS LAST LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        total = conn.execute(
            "SELECT COUNT(*) FROM metrics "
            + (" WHERE platform = ?" if platform else "")
            + (" AND fetch_status = ?" if fetch_status else ""),
            [p for i, p in enumerate([platform, fetch_status]) if p is not None],
        ).fetchone()[0]

        # R13：z-score 一律平台内部基准。可跨平台对比的只有比率类指标，
        # 原始曝光/点赞只给平台内 z，不允许跨平台直接比大小。
        normalizer = _g()["normalizer"]
        comparable = ["interaction_rate", "like_rate", "collect_rate"]
        raw_metrics = ["views", "likes", "collects", "comments", "shares"]
        platforms_present = sorted({r["platform"] for r in rows if r.get("platform")})
        stats: list[dict] = []
        stat_cache: dict[tuple[str, str], Optional[float]] = {}
        for p in platforms_present:
            for metric in comparable + raw_metrics:
                try:
                    st = normalizer.compute_platform_stats(Platform(p), metric)
                except Exception:  # noqa: BLE001 — 统计失败不能拖垮列表
                    logger.exception("平台统计计算失败 %s/%s", p, metric)
                    st = None
                if st is not None:
                    stat_cache[(p, metric)] = st.std
                    stats.append({
                        "platform": st.platform.value, "metric": st.metric,
                        "mean": round(st.mean, 4), "std": round(st.std, 4),
                        "sample_count": st.sample_count,
                    })

        for row in rows:
            z: dict[str, Optional[float]] = {}
            for metric in comparable + raw_metrics:
                if (row["platform"], metric) in stat_cache:
                    z[metric] = normalizer.z_score(
                        Platform(row["platform"]), metric, row.get(metric),
                    )
                else:
                    z[metric] = None  # 样本不足 / NULL 值 → NULL
            row["z"] = z
        return {
            "items": rows, "total": total,
            "filters": {"platform": platform, "fetch_status": fetch_status},
            "z_stats": stats,
            "z_window_days": 90,
            "cross_platform_comparable": comparable,
            "platform_only_metrics": raw_metrics,
        }

    # --- 选题评分 ---
    @app.get("/api/topics/score")
    def score_topics(limit: int = Query(20, ge=1, le=200)):
        conn = _g()["conn"]
        # 从 topic_index 取最近的选题
        rows = conn.execute(
            "SELECT id, title, dimension, source, date, first_seen_at FROM topic_index "
            "ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        topics = [
            TopicItem(
                id=r["id"], title=r["title"] or "", dimension=r["dimension"] or "",
                date=r["date"],
            )
            for r in rows
        ]
        if not topics:
            return {"mode": _g()["scoring"]._determine_mode(), "topics": [], "message": "选题库暂无数据"}
        scored = _g()["scoring"].score_topics(topics)
        return {
            "mode": scored[0].mode,
            "topics": [
                {
                    "id": s.topic.id,
                    "title": s.topic.title,
                    "dimension": s.topic.dimension,
                    "total_score": s.total_score,
                    "is_exploration": s.is_exploration,
                    "breakdown": s.score_breakdown,
                }
                for s in scored
            ],
        }

    # --- 热点雷达（R22：采集失败不显示数字）---
    @app.get("/api/hot_topics")
    def hot_topics(refresh: bool = Query(False)):
        conn = _g()["conn"]
        collector = HotTopicCollector(_g()["cfg"], conn)
        try:
            return collector.overview(force=refresh)
        except Exception as exc:  # noqa: BLE001 — 采集异常不能 500 打穿前端
            logger.exception("热点雷达采集异常")
            raise HTTPException(502, f"热点采集异常: {exc}") from exc

    @app.post("/api/hot_topics/import")
    def import_hot_topic(req: dict):
        """热点 → 选题库（写入 topic_index，可被 /api/topics/score 评分）。

        R22：必须带 source + 原文链接，否则不入库（不做无证据选题）。
        """
        conn = _g()["conn"]
        title = str((req or {}).get("title") or "").strip()
        source = str((req or {}).get("source") or "").strip()
        raw_url = str((req or {}).get("url") or "").strip()
        dimension = str((req or {}).get("dimension") or "").strip()
        if len(title) < 4:
            raise HTTPException(400, "标题过短，不能入库")
        if not source or not raw_url:
            raise HTTPException(400, "缺少来源或原文链接：热点入库必须有证据（R22）")

        topic_id = "hot-" + hashlib.sha1(
            f"{source}|{title}".encode("utf-8")
        ).hexdigest()[:12]
        existing = conn.execute(
            "SELECT id FROM topic_index WHERE id = ?", (topic_id,)
        ).fetchone()
        if existing:
            return {"id": topic_id, "imported": False, "message": "选题库已存在该热点"}

        conn.execute(
            "INSERT INTO topic_index (id, title, dimension, source, date, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                topic_id, title, dimension or None,
                f"hot:{source}", datetime.now().strftime("%Y-%m-%d"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        return {"id": topic_id, "imported": True, "title": title}

    # === 运营控制台补充路由 ===

    # --- 运营 KPI 概览（Operations 首页）---
    @app.get("/api/ops_kpi")
    def ops_kpi(days: int = Query(1, ge=1, le=365)):
        """运营 KPI。days = 统计窗口（1=今日，7/30/90 可选）。

        注意：这是**展示窗口**，与 R12 评分固定 90 天时效窗口相互独立。
        """
        conn = _g()["conn"]
        # ops_logs 统计（窗口由 days 决定；1 = 今日，R6/R16 计数口径不变）
        today_ops = conn.execute(
            "SELECT action_type, status, COUNT(*) AS n FROM ops_logs "
            "WHERE executed_at >= datetime('now', ?) GROUP BY action_type, status",
            (f"-{max(0, days - 1)} days",),
        ).fetchall()
        ops_by_type: dict[str, dict] = {}
        for r in today_ops:
            at = r["action_type"]
            ops_by_type.setdefault(at, {"success": 0, "failed": 0, "skipped": 0})
            ops_by_type[at][r["status"]] = int(r["n"])

        # 草稿状态分布
        draft_status = conn.execute(
            "SELECT status, COUNT(*) AS n FROM drafts GROUP BY status"
        ).fetchall()
        draft_map = {r["status"]: int(r["n"]) for r in draft_status}

        # 活跃熔断事件
        cb_active = conn.execute(
            "SELECT platform, reason, detected_at, cooldown_until "
            "FROM circuit_breaker_events WHERE resolved_at IS NULL"
        ).fetchall()

        # 平台活跃轮次 / 待发布草稿
        platform_stats = {}
        for p in _g()["cfg"].platforms.enabled_keys():
            rounds = conn.execute(
                "SELECT COUNT(*) FROM spiral_rounds WHERE id IN "
                "(SELECT DISTINCT round_id FROM contents WHERE id IN "
                "(SELECT content_id FROM drafts WHERE platform=?)) "
                "AND status NOT IN ('completed','partial_completed','cancelled','failed')",
                (p,),
            ).fetchone()[0]
            drafts_saved = conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE platform=? AND status='draft_saved'",
                (p,),
            ).fetchone()[0]
            drafts_published = conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE platform=? AND status='published'",
                (p,),
            ).fetchone()[0]
            broken = any(cb["platform"] == p for cb in cb_active)
            platform_stats[p] = {
                "rounds": rounds,
                "drafts_saved": drafts_saved,
                "drafts_published": drafts_published,
                "circuit_broken": broken,
            }

        return {
            "today_ops": ops_by_type,
            "window_days": days,
            "draft_status": draft_map,
            "circuit_breaker_active": [dict(r) for r in cb_active],
            "platform_stats": platform_stats,
        }

    # --- 全局运营软暂停（与熔断区分：人工主动，随时恢复）---
    @app.get("/api/ops/pause")
    def get_ops_pause():
        return pause_state(_g()["conn"]) or {"paused": False, "updated_at": None, "updated_by": None}

    @app.post("/api/ops/pause")
    def set_ops_pause(req: dict):
        paused = bool((req or {}).get("paused"))
        return set_ops_paused(_g()["conn"], paused, by="web-ui")

    # --- 通知通道自检（不落 notifications 表）---
    @app.post("/api/notify/test")
    def notify_test():
        notifier = _g().get("notifier")
        if notifier is None:
            raise HTTPException(503, "通知器未初始化")
        return {"results": notifier.send_test()}

    # --- 运营日志（ops_logs）---
    @app.get("/api/ops_logs")
    def list_ops_logs(
        platform: Optional[str] = Query(None),
        action_type: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ):
        conn = _g()["conn"]
        sql = "SELECT * FROM ops_logs WHERE 1=1"
        params: list = []
        if platform:
            sql += " AND platform = ?"; params.append(platform)
        if action_type:
            sql += " AND action_type = ?"; params.append(action_type)
        if status:
            sql += " AND status = ?"; params.append(status)
        sql += " ORDER BY COALESCE(executed_at, schedule_time) DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        return {"items": rows, "total": len(rows)}

    # --- 任务追踪（task_runs）---
    @app.get("/api/task_runs")
    def list_task_runs(
        task_type: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ):
        conn = _g()["conn"]
        sql = "SELECT * FROM task_runs WHERE 1=1"
        params: list = []
        if task_type:
            sql += " AND task_type = ?"; params.append(task_type)
        if status:
            sql += " AND status = ?"; params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    @app.post("/api/task_runs/{run_id}/resolve")
    def resolve_pending_verify(run_id: int, body: dict):
        """R17：pending_verify（崩溃前可能已发出）由用户人工核实后收口。

        body.action:
        - "sent"    用户确认已发出 → status=success（幂等留痕，不再重试）
        - "not_sent" 用户确认未发出 → status=failed（主动评论/关注不可盲目重发）
        """
        conn = _g()["conn"]
        row = conn.execute("SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "任务记录不存在")
        if row["status"] != "pending_verify":
            raise HTTPException(400, f"当前状态 {row['status']} 不是 pending_verify")
        action = (body or {}).get("action")
        target = {"sent": "success", "not_sent": "failed"}.get(action)
        if target is None:
            raise HTTPException(400, "action 必须是 sent 或 not_sent")
        note = "用户核实：已发出" if action == "sent" else "用户核实：未发出（不自动重发）"
        conn.execute(
            "UPDATE task_runs SET status = ?, finished_at = ?, error_msg = ? WHERE id = ?",
            (target, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), note, run_id),
        )
        conn.commit()
        return {"id": run_id, "status": target}

    # --- 通知中心（notifications）---
    @app.get("/api/notifications")
    def list_notifications(
        level: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
    ):
        conn = _g()["conn"]
        sql = "SELECT * FROM notifications WHERE 1=1"
        params: list = []
        if level:
            sql += " AND level = ?"; params.append(level)
        sql += " ORDER BY sent_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # --- APScheduler 定时任务状态 ---
    @app.get("/api/scheduler/jobs")
    def scheduler_jobs():
        """列出 APScheduler SQLiteJobStore 中注册的任务（含下次运行时间）。"""
        jobs = _g()["scheduler"].scheduler.get_jobs()
        result = []
        for job in jobs:
            result.append({
                "id": job.id,
                "name": job.name or job.id,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
        return {"jobs": result, "count": len(result), "running": _g()["scheduler"].scheduler.running}

    @app.post("/api/scheduler/jobs/{job_id}/run")
    def scheduler_run_job(job_id: str):
        """立即触发一次定时任务（§8.3 验证要求：手动触发并确认本地时区）。"""
        sched = _g()["scheduler"]
        job = sched.scheduler.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"任务不存在: {job_id}")
        job.modify(next_run_time=datetime.now())
        return {"job_id": job_id, "triggered_at": datetime.now().isoformat(timespec="seconds")}

    # --- 节奏调度配置（只读）---
    @app.get("/api/rhythm")
    def rhythm_config():
        """平台独立配额 / 时段窗口（RhythmController 生效中的配置）。"""
        cfg = _g()["cfg"]
        enabled = cfg.platforms.enabled_keys()
        quotas_map = getattr(cfg.rhythm, "daily_quotas", None) or {}
        windows = cfg.rhythm.windows or {}
        ops_win = windows.get("ops")
        draft_win = windows.get("draft_save")
        return {
            "enabled_platforms": enabled,
            "quotas": {
                p: {
                    "passive_replies": getattr(quotas_map.get(p), "passive_replies", None),
                    "active_comments": getattr(quotas_map.get(p), "active_comments", None),
                    "follows": getattr(quotas_map.get(p), "follows", None),
                }
                for p in enabled
            },
            "windows": {
                "ops": {
                    "start_hour": ops_win.start_hour,
                    "end_hour": ops_win.end_hour,
                } if ops_win else None,
                "draft_save": {
                    "start_hour": draft_win.start_hour,
                    "end_hour": draft_win.end_hour,
                } if draft_win else None,
            },
        }

    # --- 系统配置（G-13：白名单可写 + config_changes 审计）---
    @app.get("/api/config")
    def get_config():
        g2 = _g()
        return current_config_view(g2["cfg"], g2["conn"])

    @app.patch("/api/config")
    def patch_runtime_config(req: ConfigPatchRequest):
        """白名单字段修改：实时生效 + 持久化覆盖 + 审计留痕。

        R22：维度匹配门 / 可发布性门属硬锁，请求里出现即 400。
        """
        g2 = _g()
        try:
            result = patch_config(g2["cfg"], g2["conn"], req.updates)
        except ConfigNotAllowedError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ConfigPatchError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {**result, "config": current_config_view(g2["cfg"], g2["conn"])["editable"]}

    # --- 内容资产（G-13：外部参考 / 个人观点 / 内容模板）---
    @app.get("/api/assets")
    def list_assets(asset_type: Optional[str] = Query(None)):
        conn = _g()["conn"]
        if asset_type and asset_type not in ("reference", "voice", "template"):
            raise HTTPException(400, "asset_type 只能是 reference / voice / template")
        sql = "SELECT * FROM content_assets"
        params: list = []
        if asset_type:
            sql += " WHERE asset_type = ?"
            params.append(asset_type)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 100"
        rows = conn.execute(sql, params).fetchall()
        return {"items": [dict(r) for r in rows], "total": len(rows)}

    @app.post("/api/assets")
    def create_asset(req: CreateAssetRequest):
        """外部参考必须带可溯源链接（R22 精神：不能只存无法溯源的摘要）。"""
        if req.asset_type == "reference" and not (req.source_url or "").strip():
            raise HTTPException(422, "外部参考必须填写原始链接 source_url（可溯源）")
        conn = _g()["conn"]
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO content_assets "
            "(asset_type, title, body, source_url, platform, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (req.asset_type, req.title, req.body, req.source_url, req.platform, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM content_assets WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
        return dict(row)

    # --- 选题库 schema 检查（G-13 字段映射 tab）---
    @app.get("/api/kb/inspect")
    def kb_inspect():
        cfg = _g()["cfg"]
        try:
            return KbReader(cfg).inspect()
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(500, f"选题库检查失败：{e}")



def _find_round_for_draft(conn, draft_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT c.round_id FROM drafts d JOIN contents c ON d.content_id = c.id WHERE d.id = ?",
        (draft_id,),
    ).fetchone()
    return row[0] if row else None


app = _build_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host="127.0.0.1", port=8000, reload=False)
