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
GET  /api/goals/summary   总体目标进度（已有数据目标达成率等权平均）
POST /api/goals           创建阶段目标
GET  /api/metrics         已采集数据列表
GET  /api/circuit_breaker 熔断器状态
POST /api/circuit_breaker/recover  触发 3 步恢复
POST /api/circuit_breaker/reset     手动重置
GET  /api/gaps            缺口分析
GET  /api/topics/score    选题评分（可传 topic_ids 范围）
GET  /api/hot_topics      热点雷达（只读库）
POST /api/hot_topics/refresh  触发后台采集 + 标签匹配
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import sqlite3
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Callable, Optional

import httpx
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from src.core.config import load_config, get_config, DEFAULT_ENV_PATH
from src.core.config import PROJECT_ROOT
from src.core.tenant_registry import TenantRegistry, TenantError
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
from src.core import auth
from src.core import kb_sources
from src.core import industry_presets
from src.core.env_store import env_file, read_env, write_env
from src.core.redact import redact, redact_path
from src.core.notify import Notifier, set_active as set_active_notifier, macos_available
from src.core.hot_topics import HotTopicCollector
from src.core.recovery import RecoveryManager
from src.core.round_executor import RoundExecutor
from src.core.goal_progress import GoalProgressUpdater
from src.core.insights import PlatformInsightsEngine
from src.core.plan_version import PlanVersionManager
from src.core.ops.pause import pause_state, set_ops_paused
from src.core.config_store import (
    ConfigNotAllowedError, ConfigPatchError, apply_overrides, current_config_view,
    changed_job_ids, load_overrides, patch_config, scheduler_jobs_view,
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
_media_generation_lock = threading.Lock()

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
        # 账号组注册表是共享基础设施；默认组继续使用原有 data/ 与 backups/。
        tenant_registry = TenantRegistry(PROJECT_ROOT)
        tenant_registry.initialize()
        active_tenant_id = tenant_registry.active_id()
        active_tenant = tenant_registry.get(active_tenant_id)
        if active_tenant_id != "default":
            tenant_paths = tenant_registry.paths(
                active_tenant_id, name=str(active_tenant.get("name") or active_tenant_id),
                create=True,
            )
            # 使用绝对路径覆盖运行时路径，确保 cfg.resolve_path 不把租户目录再拼到项目根。
            cfg.paths.data_dir = str(tenant_paths.data_dir)
            cfg.paths.db_file = str(tenant_paths.db_file)
            cfg.paths.backup_dir = str(tenant_paths.backup_dir)
            cfg.paths.output_dir = str(tenant_paths.output_dir)
            cfg.paths.screenshot_dir = str(tenant_paths.screenshot_dir)
            cfg.paths.log_dir = str(tenant_paths.log_dir)
            # 选题库及浏览器登录态均按账号组隔离；平台账号 key 不与 default 共用。
            cfg.topic_kb.path = str(tenant_paths.kb_dir / "topics.csv")
        # 恢复任务只在启动且数据库尚未打开时执行；校验账号组与备份目录后再替换数据。
        runtime_data_dir = Path(cfg.resolve_path(cfg.paths.data_dir))
        runtime_backup_dir = Path(cfg.resolve_path(cfg.paths.backup_dir)).resolve()
        pending_restore = runtime_data_dir / ".pending_restore.json"
        if pending_restore.exists():
            try:
                restore_request = json.loads(pending_restore.read_text(encoding="utf-8"))
                backup_name = restore_request.get("backup_name")
                if restore_request.get("tenant_id") != active_tenant_id or not isinstance(backup_name, str):
                    raise RuntimeError("待恢复任务的账号组标识无效")
                if Path(backup_name).name != backup_name or not backup_name.startswith(f"iterwave_{active_tenant_id}_") or not backup_name.endswith(".zip"):
                    raise RuntimeError("待恢复备份名称无效")
                backup_path = (runtime_backup_dir / backup_name).resolve()
                if not backup_path.is_relative_to(runtime_backup_dir) or not backup_path.is_file():
                    raise RuntimeError("待恢复备份文件不存在或超出备份目录")
                from src.core.tenant_backup import restore_backup
                restore_backup(
                    backup_path, cfg.resolve_path(cfg.paths.db_file), runtime_data_dir,
                    runtime_backup_dir, active_tenant_id,
                )
                pending_restore.unlink(missing_ok=True)
                logger.warning("账号组快照恢复完成 tenant=%s backup=%s", active_tenant_id, backup_name)
            except Exception as exc:  # noqa: BLE001 — 恢复失败必须阻止启动，不能悄悄使用旧库
                logger.exception("账号组快照恢复失败，拒绝启动 tenant=%s", active_tenant_id)
                raise RuntimeError(f"账号组恢复失败，应用未启动：{exc}") from exc
        # G-13：先套用 Web 端持久化的覆盖值（租户各自的 data/config_overrides.json），
        # 再构造各模块 —— 保证 scheduler/adapter/ops 拿到的是同一份生效配置
        applied_overrides = apply_overrides(cfg, load_overrides(cfg))
        if applied_overrides:
            logger.info("⚙️ 已套用 %d 项运行时配置覆盖：%s",
                        len(applied_overrides), applied_overrides)
        if active_tenant_id != "default":
            # 在覆盖值应用后再次强制命名空间化，避免旧配置覆盖把登录态指回 default。
            for platform_cfg in cfg.platforms.model_dump().keys():
                item = getattr(cfg.platforms, platform_cfg)
                prefix = f"{active_tenant_id}__"
                if not item.account_key.startswith(prefix):
                    item.account_key = f"{prefix}{item.account_key}"
            # 租户选题库路径是隔离边界；不能让历史 config_overrides.json 把它指回共享或其他租户目录。
            cfg.topic_kb.path = str(tenant_paths.kb_dir / "topics.csv")
        # 登录二维码、持久化浏览器 profile、storage_state 必须共用当前账号组数据根目录。
        from src.core import account_login, login_manager
        runtime_data_dir = cfg.resolve_path(cfg.paths.data_dir)
        account_login.configure_data_dir(runtime_data_dir)
        login_manager.configure_data_dir(runtime_data_dir)
        db = Database(cfg)
        db.initialize()
        # 线程安全包装：路由线程池 / 调度器 / 执行器共用这一个连接
        from src.core.db import LockedConnection
        conn = LockedConnection(db.connect())

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

        _g_ctx.clear()
        _g_ctx.update({
            "tenant_registry": tenant_registry,
            "runtime_tenant_id": active_tenant_id,
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
        scheduler.wire(
            conn=conn, adapters=adapters, sm_draft=sm_draft, sm_round=sm_round,
            circuit_breaker=cb, notifier=notifier,
            ops=ops, kb_reader=None, spiral=spiral, scoring=scoring,
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

        # 自动闭环：复盘结论 → 下一轮（定时任务 autogrow_next_round 驱动）
        from ..core.autogrow import AutogrowController
        autogrow = AutogrowController(
            cfg, conn, executor, goal_progress=_g_ctx["goal_progress"],
            notifier=notifier, guard=guard, pvm=pvm,
        )
        _g_ctx["autogrow"] = autogrow
        scheduler.autogrow = autogrow

        # 热点：库里没有新鲜数据时启动后补跑一次后台采集（不阻塞启动）
        from ..core import hot_topic_service
        if cfg.hot_topics.enabled is not False and hot_topic_service.is_stale(cfg, conn):
            hot_topic_service.trigger_background(cfg, trigger="startup")

        logger.info("✅ FastAPI 应用启动完成，%d 个模块已加载", len(_g_ctx))

        try:
            yield
        finally:
            scheduler.shutdown()
            conn.close()
            logger.info("⏹️ FastAPI 应用关闭")

    app = FastAPI(
        title="IterWave — 四平台图文运营 Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    _register_routes(app)

    # === 访问保护（P0）：口令设置后 /api/* 必须登录；脚本可带 X-Access-Token ===
    @app.middleware("http")
    async def access_guard_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Vite 子路径构建会把 API 地址也加上 /<base>；这里在路由分发前剥离前缀，
        # 同时设置 root_path 保留外部 URL，兼容直连子路径与反向代理已剥离前缀两种部署。
        base_prefix = (os.getenv("APP_BASE_PATH") or os.getenv("VITE_BASE") or "").strip("/")
        raw_path = request.scope.get("path", "/")
        prefix = f"/{base_prefix}" if base_prefix else ""
        if prefix and (raw_path == prefix or raw_path.startswith(prefix + "/")):
            request.scope["path"] = raw_path[len(prefix):] or "/"
            current_root = str(request.scope.get("root_path") or "").rstrip("/")
            request.scope["root_path"] = f"{current_root}{prefix}"
        path = request.scope.get("path", "/")
        if path.startswith("/api/"):
            # 上传接口的 base64 JSON 约 28MB；其它 API 不应接受无限大的请求体。
            max_body_bytes = 30 * 1024 * 1024 if path == "/api/kb/sources/upload" else 17 * 1024 * 1024 if path == "/api/media" else 5 * 1024 * 1024
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    return JSONResponse({"detail": "Content-Length 不合法"}, status_code=400)
                if declared_size < 0 or declared_size > max_body_bytes:
                    return JSONResponse(
                        {"detail": f"请求体超过 {max_body_bytes // (1024 * 1024)}MB 上限"},
                        status_code=413,
                    )
        if path.startswith("/api/") and path not in ("/api/auth/status", "/api/auth/login"):
            # 未设置口令时，仅显式启用本机引导且确认无代理头的 loopback 请求可跳过初始化认证。
            # 其它访问必须通过首次设置引导口令；设置口令后所有请求仍需认证。
            local_direct = auth.is_trusted_local_setup(request)

            if not auth.password_configured() and path != "/api/auth/password":
                # 未设密码 + 本机直连 → 放行（方便本地开发）；公网部署 → 拦截 403
                if not local_direct:
                    return JSONResponse(
                        {"detail": "请先完成访问口令设置", "code": "setup_required"},
                        status_code=403,
                    )
            # 仅显式启用本机引导时，未设密码的 loopback 请求可跳过初始化认证。
            # 其它首次设置交给该端点校验服务器引导令牌，不能先要求已有登录态。
            if not auth.password_configured() and local_direct:
                pass
            elif not auth.password_configured() and path == "/api/auth/password":
                pass
            else:
                cookie_token = request.cookies.get(auth.COOKIE_NAME)
                client_ip = request.client.host if request.client else "unknown"
                has_token_header = bool(request.headers.get("x-access-token")) or (
                    (request.headers.get("authorization") or "").lower().startswith("bearer ")
                )
                limited_before = has_token_header and auth.is_rate_limited(client_ip)
                if not auth.is_authenticated(cookie_token, request.headers, ip=client_ip):
                    return JSONResponse(
                        {
                            "detail": "认证尝试次数过多，请 10 分钟后再试" if limited_before else "需要登录",
                            "code": "auth_rate_limited" if limited_before else "auth_required",
                        },
                        status_code=429 if limited_before else 401,
                    )

        # Content-Length 可被省略或伪造；已通过认证的写请求还要按实际流量限额，
        # 避免 chunked 上传绕过大小限制后耗尽内存。
        if path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH"):
            max_body_bytes = (
                30 * 1024 * 1024 if path == "/api/kb/sources/upload"
                else 17 * 1024 * 1024 if path == "/api/media"
                else 5 * 1024 * 1024
            )
            chunks: list[bytes] = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > max_body_bytes:
                    return JSONResponse(
                        {"detail": f"请求体超过 {max_body_bytes // (1024 * 1024)}MB 上限"},
                        status_code=413,
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            request._body = body
            body_sent = False

            async def replay_body():
                nonlocal body_sent
                if body_sent:
                    return {"type": "http.disconnect"}
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = replay_body

        return await call_next(request)

    # === 挂载 React 前端静态文件 (同 origin, 单进程) ===
    dist_dir = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    if dist_dir.is_dir():
        # assets 子目录（JS/CSS/图片）
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
            # 子路径部署（VITE_BASE=/iterwave/）：同一份产物也能在 /iterwave 下取到资源
            base_prefix = (os.getenv("APP_BASE_PATH") or os.getenv("VITE_BASE") or "").strip("/")
            if base_prefix:
                app.mount(f"/{base_prefix}/assets", StaticFiles(directory=str(assets_dir)),
                          name="assets_prefixed")
                logger.info("📦 子路径部署前缀：/%s", base_prefix)

        # SPA fallback —— middleware 方式，不拦截 API 路径的尾部斜杠 307 重定向
        # 之前用 catch-all GET /{full_path:path} 会抢先匹配 /api/* 带尾部斜杠的请求，
        # 阻止 FastAPI 正常的 307 重定向行为 → 前端调用返回 404
        _SPA_PROTECTED_PREFIXES = (
            "/api/", "/health", "/docs", "/openapi.json", "/redoc", "/favicon",
        )

        @app.middleware("http")
        async def spa_fallback_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
            response = await call_next(request)
            # 只对 GET/HEAD + 404 做 SPA 兜底，其余原样返回
            if request.method not in ("GET", "HEAD") or response.status_code != 404:
                return response
            path = request.scope.get("path", "/")
            # API/文档等保留路径不兜底（返回 FastAPI 原始 404 JSON）
            if path.startswith(_SPA_PROTECTED_PREFIXES):
                return response
            # 根路径 / 直接回 index.html
            if path == "/":
                return FileResponse(str(dist_dir / "index.html"))
            # 根目录静态文件（favicon.svg / robots.txt 等）优先返回真实文件
            safe_path = path.lstrip("/")
            if safe_path:
                candidate = (dist_dir / safe_path).resolve()
                try:
                    if candidate.is_file() and dist_dir.resolve() in candidate.parents:
                        return FileResponse(str(candidate))
                except (ValueError, OSError):
                    pass
            # SPA 路由（/dashboard, /plans 等）兜底回 index.html
            return FileResponse(str(dist_dir / "index.html"))

        logger.info("📦 前端静态文件已挂载 (middleware SPA fallback): %s", dist_dir)
    else:
        logger.warning("⚠️  web/dist 不存在，前端未挂载（请先 `cd web && npm run build`）")

    return app


# =====================================================================
# Pydantic 请求模型
# =====================================================================

class MediaGenerationSettingsRequest(BaseModel):
    enabled: bool = False
    monthly_quota: int = Field(default=20, ge=0, le=1000)
    prompt_prefix: str = Field(default="", max_length=2000)
    model: str = Field(default="gpt-image-1", min_length=1, max_length=120)
    size: str = Field(default="1024x1024", pattern="^(1024x1024|1024x1536|1536x1024)$")


class MediaGenerationRequest(BaseModel):
    prompt: str = Field(min_length=5, max_length=2000)
    title: str = Field(default="", max_length=200)
    alt_text: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=30)


class TenantBackupRestoreRequest(BaseModel):
    backup_name: str = Field(min_length=1, max_length=255)
    confirm: bool = False


class MediaAssetReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str = Field(default="", max_length=500)


class MediaAssetUseRequest(BaseModel):
    content_id: str = Field(min_length=1, max_length=128)
    usage_role: str = Field(default="illustration", pattern="^(cover|illustration|background)$")


class MediaAssetUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    title: str = Field(default="", max_length=200)
    alt_text: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=30)
    data_base64: str = Field(min_length=1)


class TenantCreateRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=80)


class TenantSelectRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=48)


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
    goal_level: str = "platform"
    parent_goal_id: Optional[str] = None
    weight: Optional[float] = Field(None, gt=0, le=100)
    contribution_note: Optional[str] = Field(None, max_length=200)

    @field_validator("goal_level")
    @classmethod
    def _level_enum(cls, v: str) -> str:
        if v not in ("north_star", "platform"):
            raise ValueError("goal_level 只能是 north_star（总目标）或 platform（平台目标）")
        return v

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

    @field_validator("target_platform")
    @classmethod
    def _platform_enum(cls, v: Optional[str]) -> Optional[str]:
        """平台决定统计口径：只接受四平台，空串归一为 None（= 全平台汇总）。"""
        if v is None or not str(v).strip():
            return None
        if v not in ("xhs", "douyin", "kuaishou", "wechat_channels"):
            raise ValueError("target_platform 必须是 xhs / douyin / kuaishou / wechat_channels，或留空表示全平台")
        return v


class GoalUpdateRequest(BaseModel):
    """编辑阶段目标：只允许改口径字段与状态；current_value 由采集回写（R21）。"""

    goal_text: Optional[str] = None
    target_metric: Optional[str] = None
    target_value: Optional[float] = None
    target_platform: Optional[str] = None
    metric_scope: Optional[str] = None
    baseline_value: Optional[float] = None
    baseline_at: Optional[str] = None
    metric_period_start: Optional[str] = None
    metric_period_end: Optional[str] = None
    data_source: Optional[str] = None
    status: Optional[str] = None
    goal_level: Optional[str] = None
    parent_goal_id: Optional[str] = None
    weight: Optional[float] = Field(None, gt=0, le=100)
    contribution_note: Optional[str] = Field(None, max_length=200)

    @field_validator("goal_level")
    @classmethod
    def _level_enum(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("north_star", "platform"):
            raise ValueError("goal_level 只能是 north_star 或 platform")
        return v

    @field_validator("goal_text", "target_metric", "baseline_at",
                     "metric_period_start", "metric_period_end", "data_source")
    @classmethod
    def _not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not str(v).strip():
            raise ValueError("R21 口径字段不能为空（不修改就不要传该字段）")
        return v

    @field_validator("metric_scope")
    @classmethod
    def _scope_enum(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("delta", "total"):
            raise ValueError("metric_scope 只能是 delta 或 total")
        return v

    @field_validator("target_value")
    @classmethod
    def _target_positive(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("target_value 必须大于 0")
        return v

    @field_validator("status")
    @classmethod
    def _status_enum(cls, v: Optional[str]) -> Optional[str]:
        allowed = ("active", "completed", "failed", "cancelled", "paused")
        if v is not None and v not in allowed:
            raise ValueError(f"status 只能是 {' / '.join(allowed)}")
        return v

    @field_validator("target_platform")
    @classmethod
    def _platform_enum(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        if v not in ("xhs", "douyin", "kuaishou", "wechat_channels"):
            raise ValueError("target_platform 必须是 xhs / douyin / kuaishou / wechat_channels，或留空表示全平台")
        return v


class LlmKeyRequest(BaseModel):
    """R23：Key 只写 .env（chmod 600），永不回传、不落库、不写日志。空串 = 删除。"""

    api_key: str = Field(default="")


class CreateRoundRequest(BaseModel):
    goal_id: Optional[str] = None
    topic_count: int = 5

class NotifyRuleRequest(BaseModel):
    scenario: str
    channel: str
    level: str
    enabled: bool = True
    condition: Optional[str] = None
    notes: Optional[str] = None
    platforms: Optional[list[str]] = None

    @field_validator("scenario")
    @classmethod
    def _scenario_ok(cls, v: str) -> str:
        text = v.strip()
        if not text or len(text) > 64:
            raise ValueError("scenario 必须是 1-64 字的场景标识")
        return text

    @field_validator("channel")
    @classmethod
    def _channel_enum(cls, v: str) -> str:
        if v not in ("macos", "wecom", "feishu", "console"):
            raise ValueError("channel 只能是 macos / wecom / feishu / console")
        return v

    @field_validator("level")
    @classmethod
    def _level_enum(cls, v: str) -> str:
        if v not in ("info", "warning", "error", "critical"):
            raise ValueError("level 只能是 info / warning / error / critical")
        return v


class PlatformStyleUpdate(BaseModel):
    """平台文案风格（platform_styles 表）：只提交改动字段。"""

    title_style: Optional[str] = Field(None, max_length=200)
    body_style: Optional[str] = Field(None, max_length=200)
    tone: Optional[str] = Field(None, max_length=200)
    hashtags: Optional[list[str]] = Field(None, max_length=10)


class PublishConfirmRequest(BaseModel):
    """发布确认补充：笔记链接；质检/人工审核/平台版本异常均需单独说明例外原因。"""

    note_url: Optional[str] = Field(None, max_length=500)
    quality_override_reason: Optional[str] = Field(None, max_length=500)
    platform_sync_override_reason: Optional[str] = Field(None, max_length=500)


class PlatformSyncConfirmRequest(BaseModel):
    """用户确认已在平台端手动同步为当前本地文案版本。"""

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("平台版本同步说明不能为空")
        return value


class TransitionRequest(BaseModel):
    target_status: str
    reason: str = ""


class RunRoundRequest(BaseModel):
    """不传 = 按母计划 + Agent 编排执行；传了则本次覆盖。"""
    topic_count: Optional[int] = Field(None, ge=1, le=50)
    platforms: Optional[list[str]] = None
    save_drafts: Optional[bool] = None


class IdsRequest(BaseModel):
    ids: list[str] = Field(min_length=1)
    reason: str = ""


class CreateTopicRequest(BaseModel):
    title: str
    dimension: str
    summary: str = ""
    link: str = ""


class OrchestrationRequest(BaseModel):
    steps: list[dict]


class DiagnoseRequest(BaseModel):
    live_llm: bool = False


class UpdatePlanRequest(BaseModel):
    """G-09 总计划（§19）：只允许改签名覆盖的 4 个字段 + 维度分布。"""

    topic_count_per_platform: int = Field(ge=1, le=50)
    dimension_allocation: dict[str, int] = Field(default_factory=dict)
    enabled_platforms: Optional[list[str]] = None
    content_format: str = "image_text"
    # 平台分项计划：{平台: {topic_count, publish_slots: ["20:00"]}}
    per_platform: dict[str, dict] = Field(default_factory=dict)
    # 节奏：{round_interval_days, rounds_planned, goal_id}
    cadence: dict = Field(default_factory=dict)

    @field_validator("per_platform")
    @classmethod
    def _valid_per_platform(cls, v: dict[str, dict]) -> dict[str, dict]:
        import re as _re
        valid = {p.value for p in Platform}
        for p, conf in v.items():
            if p not in valid:
                raise ValueError(f"未知平台: {p}")
            tc = conf.get("topic_count")
            if tc not in (None, "") and (not isinstance(tc, int) or isinstance(tc, bool) or tc < 1):
                raise ValueError(f"{p} 篇数必须是正整数")
            for slot in conf.get("publish_slots") or []:
                if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(slot).strip()):
                    raise ValueError(f"{p} 发布时段格式应为 HH:MM，收到 {slot}")
        return v

    @field_validator("cadence")
    @classmethod
    def _valid_cadence(cls, v: dict) -> dict:
        out = {}
        if v.get("round_interval_days") not in (None, ""):
            d = float(v["round_interval_days"])
            if not 0.5 <= d <= 90:
                raise ValueError("轮间隔必须在 0.5-90 天之间")
            out["round_interval_days"] = d
        if v.get("rounds_planned") not in (None, ""):
            n = int(v["rounds_planned"])
            if not 1 <= n <= 200:
                raise ValueError("计划轮数必须在 1-200 之间")
            out["rounds_planned"] = n
        if v.get("goal_id"):
            out["goal_id"] = str(v["goal_id"])
        return out

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


class ResubmitContentRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("重新提交审核的理由不能为空")
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

    def _generate_media_asset(req: MediaGenerationRequest, source_asset_id: int | None = None):
        registry = _g().get("tenant_registry")
        tenant_id = _g().get("runtime_tenant_id", "default")
        conn = _g()["conn"]
        if registry is None:
            raise HTTPException(503, "账号组注册表尚未初始化")
        settings_row = conn.execute(
            "SELECT enabled, monthly_quota, prompt_prefix, model, size "
            "FROM media_generation_settings WHERE id = 1"
        ).fetchone()
        settings = dict(settings_row) if settings_row else {
            "enabled": 0, "monthly_quota": 20, "prompt_prefix": "",
            "model": os.getenv("ITERWAVE_IMAGE_MODEL", "gpt-image-1"),
            "size": "1024x1024",
        }
        if not settings["enabled"]:
            raise HTTPException(409, "AI 配图功能尚未启用，请先在图片素材库设置中启用")
        prompt = req.prompt.strip()
        final_prompt = "\n\n".join(
            part for part in (str(settings.get("prompt_prefix") or "").strip(), prompt) if part
        )
        model = str(settings.get("model") or os.getenv("ITERWAVE_IMAGE_MODEL", "gpt-image-1"))
        size = str(settings.get("size") or "1024x1024")
        paths = registry.paths(tenant_id, create=True)
        source_path = None
        source_mime = None
        source_filename = None
        if source_asset_id is not None:
            source = conn.execute(
                "SELECT stored_filename, original_filename, mime_type, status, review_status "
                "FROM media_assets WHERE id = ?", (source_asset_id,),
            ).fetchone()
            if source is None or source["status"] != "active" or source["review_status"] != "approved":
                raise HTTPException(409, "只能基于已审核通过且未归档的素材进行改图")
            source_path = (paths.media_dir / source["stored_filename"]).resolve()
            if not source_path.is_relative_to(paths.media_dir.resolve()) or not source_path.is_file():
                raise HTTPException(404, "原始素材文件不存在")
            source_mime = source["mime_type"]
            source_filename = source["original_filename"]

        # 整个配额检查与调用串行化，避免并发请求同时越过月度上限。
        with _media_generation_lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            used = int(conn.execute(
                "SELECT COUNT(*) FROM media_generation_jobs WHERE created_at >= ?", (month_start,),
            ).fetchone()[0])
            quota = int(settings.get("monthly_quota") or 0)
            if quota <= 0 or used >= quota:
                raise HTTPException(429, f"本账号组本月 AI 配图额度已用完（{used}/{quota}）")
            cur = conn.execute(
                "INSERT INTO media_generation_jobs (prompt, final_prompt, model, status, created_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (prompt, final_prompt, model, now),
            )
            job_id = int(cur.lastrowid)
            conn.commit()
            target = None
            try:
                from src.core.media_generation import ImageGenerationError, generate_image_bytes
                from src.core.media_quality import score_image
                raw, mime_type, suffix, width, height = generate_image_bytes(
                    final_prompt, model=model, size=size, source_path=source_path,
                    source_mime_type=source_mime, source_filename=source_filename,
                )
                stored_filename = f"{uuid.uuid4().hex}{suffix}"
                target = (paths.media_dir / stored_filename).resolve()
                if not target.is_relative_to(paths.media_dir.resolve()):
                    raise HTTPException(422, "生成文件路径无效")
                target.write_bytes(raw)
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
                title = req.title.strip() or prompt[:80]
                tags = list(dict.fromkeys(tag.strip()[:60] for tag in req.tags if tag.strip()))[:30]
                source_ref = f"job:{job_id}" if source_asset_id is None else f"job:{job_id};edit_of:{source_asset_id}"
                quality = score_image(raw)
                asset_cur = conn.execute(
                    "INSERT INTO media_assets "
                    "(title, original_filename, stored_filename, mime_type, byte_size, sha256, "
                    "width, height, alt_text, tags, source_type, source_ref, status, review_status, "
                    "quality_score, quality_details) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generated', ?, 'active', 'pending_review', ?, ?)",
                    (
                        title, stored_filename, stored_filename, mime_type, len(raw),
                        hashlib.sha256(raw).hexdigest(), width, height, req.alt_text.strip(),
                        json.dumps(tags, ensure_ascii=False), source_ref, quality["score"],
                        json.dumps(quality, ensure_ascii=False),
                    ),
                )
                asset_id = int(asset_cur.lastrowid)
                conn.execute(
                    "UPDATE media_generation_jobs SET status = 'succeeded', output_asset_id = ?, "
                    "completed_at = ? WHERE id = ?",
                    (asset_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job_id),
                )
                conn.commit()
                return {
                    "id": asset_id, "title": title, "original_filename": stored_filename,
                    "mime_type": mime_type, "byte_size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "width": width, "height": height, "alt_text": req.alt_text.strip(),
                    "tags": tags, "source_type": "generated", "source_ref": source_ref,
                    "quality_score": quality["score"], "quality_details": quality,
                    "status": "active", "review_status": "pending_review", "review_note": None,
                    "duplicate": False, "file_url": f"/api/media/{asset_id}/file",
                    "job_id": job_id, "review_required": True,
                }
            except HTTPException:
                if target is not None:
                    target.unlink(missing_ok=True)
                conn.execute(
                    "UPDATE media_generation_jobs SET status = 'failed', error_message = ?, completed_at = ? WHERE id = ?",
                    ("图片生成请求未完成", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job_id),
                )
                conn.commit()
                raise
            except Exception as exc:  # noqa: BLE001 — 保存失败必须记账并留审计
                if target is not None:
                    target.unlink(missing_ok=True)
                conn.execute(
                    "UPDATE media_generation_jobs SET status = 'failed', error_message = ?, completed_at = ? WHERE id = ?",
                    (str(exc)[:500], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job_id),
                )
                conn.commit()
                logger.exception("AI 配图任务失败 job=%s", job_id)
                from src.core.media_generation import ImageGenerationError
                if isinstance(exc, ImageGenerationError):
                    raise HTTPException(502, str(exc)) from exc
                raise HTTPException(500, "图片生成失败，任务已记录") from exc

    @app.get("/api/media/generation-settings")
    def get_media_generation_settings():
        conn = _g()["conn"]
        row = conn.execute(
            "SELECT enabled, monthly_quota, prompt_prefix, model, size, updated_at "
            "FROM media_generation_settings WHERE id = 1"
        ).fetchone()
        settings = dict(row) if row else {
            "enabled": False, "monthly_quota": 20, "prompt_prefix": "",
            "model": os.getenv("ITERWAVE_IMAGE_MODEL", "gpt-image-1"),
            "size": "1024x1024", "updated_at": None,
        }
        settings["enabled"] = bool(settings["enabled"])
        month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        settings["used_this_month"] = int(conn.execute(
            "SELECT COUNT(*) FROM media_generation_jobs WHERE created_at >= ?", (month_start,),
        ).fetchone()[0])
        key_env = os.getenv("ITERWAVE_IMAGE_API_KEY_ENV", "OPENAI_API_KEY").strip()
        settings["provider_configured"] = bool(key_env and os.getenv(key_env))
        settings["api_base_url"] = os.getenv("ITERWAVE_IMAGE_API_BASE_URL", "https://api.openai.com/v1")
        settings["api_key_env"] = key_env
        return settings

    @app.put("/api/media/generation-settings")
    def put_media_generation_settings(req: MediaGenerationSettingsRequest):
        conn = _g()["conn"]
        conn.execute(
            "INSERT INTO media_generation_settings (id, enabled, monthly_quota, prompt_prefix, model, size, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, monthly_quota=excluded.monthly_quota, "
            "prompt_prefix=excluded.prompt_prefix, model=excluded.model, size=excluded.size, updated_at=CURRENT_TIMESTAMP",
            (int(req.enabled), req.monthly_quota, req.prompt_prefix.strip(), req.model.strip(), req.size),
        )
        conn.commit()
        return get_media_generation_settings()

    @app.post("/api/media/generate", status_code=201)
    def generate_media_asset(req: MediaGenerationRequest):
        return _generate_media_asset(req)

    @app.post("/api/media/{asset_id}/edit", status_code=201)
    def edit_media_asset(asset_id: int, req: MediaGenerationRequest):
        return _generate_media_asset(req, source_asset_id=asset_id)

    @app.post("/api/media/{asset_id}/review")
    def review_media_asset(asset_id: int, req: MediaAssetReviewRequest):
        conn = _g()["conn"]
        row = conn.execute(
            "SELECT id, review_status, status FROM media_assets WHERE id = ?", (asset_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "图片素材不存在")
        conn.execute(
            "UPDATE media_assets SET review_status = ?, review_note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (req.decision, req.note.strip(), asset_id),
        )
        conn.commit()
        return {
            "id": asset_id, "review_status": req.decision, "review_note": req.note.strip(),
            "previous_review_status": row["review_status"],
        }

    @app.get("/api/media")
    def list_media_assets(
        query: Optional[str] = Query(None, max_length=200),
        status: str = Query("active", pattern="^(active|archived|all)$"),
        review_status: str = Query("all", pattern="^(pending_review|approved|rejected|all)$"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        conn = _g()["conn"]
        where: list[str] = []
        params: list = []
        if status != "all":
            where.append("status = ?")
            params.append(status)
        if review_status != "all":
            where.append("review_status = ?")
            params.append(review_status)
        if query and query.strip():
            needle = f"%{query.strip()}%"
            where.append("(title LIKE ? OR original_filename LIKE ? OR alt_text LIKE ? OR tags LIKE ?)")
            params.extend([needle, needle, needle, needle])
        sql = (
            "SELECT id, title, original_filename, mime_type, byte_size, sha256, "
            "width, height, alt_text, tags, source_type, source_ref, status, review_status, review_note, "
            "quality_score, quality_details, created_at, updated_at "
            "FROM media_assets"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(item["tags"] or "[]")
            except (TypeError, ValueError):
                item["tags"] = []
            try:
                item["quality_details"] = json.loads(item["quality_details"]) if item.get("quality_details") else None
            except (TypeError, ValueError):
                item["quality_details"] = None
            item["file_url"] = f"/api/media/{item['id']}/file"
            result.append(item)
        return {"items": result, "limit": limit, "offset": offset}

    @app.post("/api/media", status_code=201)
    def upload_media_asset(req: MediaAssetUploadRequest):
        registry = _g().get("tenant_registry")
        tenant_id = _g().get("runtime_tenant_id", "default")
        if registry is None:
            raise HTTPException(503, "账号组注册表尚未初始化")
        try:
            paths = registry.paths(tenant_id, create=True)
            raw = base64.b64decode(req.data_base64, validate=True)
        except (ValueError, TenantError) as exc:
            raise HTTPException(422, "图片数据无效或账号组目录不可用") from exc
        if not raw or len(raw) > 12 * 1024 * 1024:
            raise HTTPException(413, "图片大小必须大于 0 且不超过 12MB")
        digest = hashlib.sha256(raw).hexdigest()
        conn = _g()["conn"]
        existing = conn.execute(
            "SELECT id, title, original_filename, mime_type, byte_size, width, height, "
            "alt_text, tags, status, review_status, review_note, created_at FROM media_assets "
            "WHERE sha256 = ? AND status = 'active' LIMIT 1",
            (digest,),
        ).fetchone()
        if existing:
            item = dict(existing)
            item["tags"] = json.loads(item["tags"] or "[]")
            item["duplicate"] = True
            item["file_url"] = f"/api/media/{item['id']}/file"
            return item
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise HTTPException(422, "上传内容不是有效的图片文件或超过安全像素限制") from exc
        format_map = {
            "JPEG": ("image/jpeg", ".jpg"),
            "PNG": ("image/png", ".png"),
            "WEBP": ("image/webp", ".webp"),
            "GIF": ("image/gif", ".gif"),
        }
        if image_format not in format_map:
            raise HTTPException(415, "仅支持 JPEG、PNG、WebP、GIF 图片")
        mime_type, suffix = format_map[image_format]
        if width < 1 or height < 1 or width > 20000 or height > 20000 or width * height > 40_000_000:
            raise HTTPException(422, "图片尺寸不在允许范围内（单边不超过 20000 像素，总像素不超过 4000 万）")
        stored_filename = f"{uuid.uuid4().hex}{suffix}"
        target = (paths.media_dir / stored_filename).resolve()
        if not target.is_relative_to(paths.media_dir.resolve()):
            raise HTTPException(422, "素材文件路径无效")
        safe_filename = Path(req.filename.replace(chr(92), "/")).name[:255]
        title = req.title.strip() or safe_filename[:200]
        tags = list(dict.fromkeys(tag.strip()[:60] for tag in req.tags if tag.strip()))[:30]
        from src.core.media_quality import score_image
        quality = score_image(raw)
        target.write_bytes(raw)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        try:
            cur = conn.execute(
                "INSERT INTO media_assets "
                "(title, original_filename, stored_filename, mime_type, byte_size, sha256, "
                "width, height, alt_text, tags, source_type, status, quality_score, quality_details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'upload', 'active', ?, ?)",
                (
                    title, safe_filename, stored_filename, mime_type,
                    len(raw), digest, width, height, req.alt_text.strip(),
                    json.dumps(tags, ensure_ascii=False), quality["score"],
                    json.dumps(quality, ensure_ascii=False),
                ),
            )
            conn.commit()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {
            "id": cur.lastrowid, "title": title,
            "original_filename": Path(req.filename).name[:255],
            "mime_type": mime_type, "byte_size": len(raw), "sha256": digest,
            "width": width, "height": height, "alt_text": req.alt_text.strip(),
            "tags": tags, "source_type": "upload", "status": "active",
            "review_status": "approved", "review_note": None,
            "quality_score": quality["score"], "quality_details": quality,
            "duplicate": False, "file_url": f"/api/media/{cur.lastrowid}/file",
        }

    @app.get("/api/media/{asset_id}/file")
    def get_media_asset_file(asset_id: int):
        registry = _g().get("tenant_registry")
        tenant_id = _g().get("runtime_tenant_id", "default")
        if registry is None:
            raise HTTPException(503, "账号组注册表尚未初始化")
        row = _g()["conn"].execute(
            "SELECT stored_filename, mime_type, status FROM media_assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
        if row is None or row["status"] != "active":
            raise HTTPException(404, "图片素材不存在或已归档")
        paths = registry.paths(tenant_id)
        target = (paths.media_dir / row["stored_filename"]).resolve()
        if not target.is_relative_to(paths.media_dir.resolve()) or not target.is_file():
            raise HTTPException(404, "图片文件不存在")
        return FileResponse(
            target, media_type=row["mime_type"],
            headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/media/targets")
    def list_media_usage_targets():
        rows = _g()["conn"].execute(
            "SELECT c.id, c.title, c.platform, c.round_id, c.created_at, "
            "c.visual_status, c.visual_reason, d.status AS draft_status "
            "FROM contents c JOIN drafts d ON d.content_id = c.id "
            "WHERE d.status IN ('draft_pending','draft_saved') "
            "ORDER BY c.created_at DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(row) for row in rows]}

    @app.get("/api/media/recommendations")
    def recommend_media_assets(
        content_id: str = Query(..., min_length=1, max_length=128),
        usage_role: str = Query("cover", pattern="^(cover|illustration|background)$"),
        limit: int = Query(8, ge=1, le=20),
    ):
        """基于内容文本与素材标题/标签的可解释关键词匹配；只推荐已审核且未归档素材，不自动分配。"""
        conn = _g()["conn"]
        content = conn.execute(
            "SELECT id, title, body, platform FROM contents WHERE id = ?", (content_id,),
        ).fetchone()
        if content is None:
            raise HTTPException(404, "目标内容不存在")
        # 中文按 2–4 字滑动片段匹配，英文/数字按词匹配；这是轻量启发式，不调用外部模型。
        import re
        source_text = " ".join(str(content[key] or "") for key in ("title", "body")).lower()
        tokens = set(re.findall(r"[a-z0-9]{2,}", source_text))
        for run in re.findall(r"[\u4e00-\u9fff]+", source_text):
            for size in (2, 3, 4):
                tokens.update(run[index:index + size] for index in range(max(0, len(run) - size + 1)))
        stopwords = {"我们", "你们", "他们", "这个", "那个", "可以", "如何", "因为", "所以", "以及", "但是", "如果", "门店", "内容", "介绍", "推荐"}
        tokens = {token for token in tokens if token not in stopwords}
        rows = conn.execute(
            "SELECT id, title, original_filename, alt_text, tags, quality_score, width, height "
            "FROM media_assets WHERE status = 'active' AND review_status = 'approved' "
            "ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
        purpose_terms = {
            "cover": {"封面", "主图", "门头", "产品", "招牌"},
            "illustration": {"正文", "细节", "环境", "过程", "服务", "产品"},
            "background": {"背景", "底图", "纹理", "氛围"},
        }[usage_role]
        ranked = []
        for row in rows:
            try:
                tags = json.loads(row["tags"] or "[]")
                if not isinstance(tags, list): tags = []
            except (TypeError, json.JSONDecodeError):
                tags = []
            searchable = " ".join([str(row["title"] or ""), str(row["original_filename"] or ""), str(row["alt_text"] or ""), *[str(tag) for tag in tags]]).lower()
            matched = sorted(token for token in tokens if token in searchable)
            purpose_hits = sorted(term for term in purpose_terms if term in searchable)
            # 图片质量只能用于同等相关性下的排序，不能让无关图片仅凭质量分进入推荐。
            if not matched and not purpose_hits:
                continue
            score = len(matched) * 10 + len(purpose_hits) * 3 + (max(0, int(row["quality_score"] or 0)) // 20)
            ranked.append({
                "asset_id": int(row["id"]), "title": row["title"],
                "score": score, "matched_keywords": matched[:8],
                "purpose_matches": purpose_hits, "quality_score": row["quality_score"],
                "width": row["width"], "height": row["height"],
                "reason": ("匹配关键词：" + "、".join(matched[:5]) if matched else "用途标签匹配")
                    + (("；用途：" + "、".join(purpose_hits)) if purpose_hits else ""),
            })
        ranked.sort(key=lambda item: (-item["score"], -(item["quality_score"] or 0), item["asset_id"]))
        return {
            "content_id": content_id, "usage_role": usage_role,
            "method": "keyword_match", "automatic_assignment": False,
            "items": ranked[:limit],
            "message": "这是基于标题、正文、素材名称和标签的关键词推荐；无匹配项不代表素材不可用。推荐结果不会自动配图。",
        }

    @app.post("/api/media/{asset_id}/use")
    def use_media_asset(asset_id: int, req: MediaAssetUseRequest):
        registry = _g().get("tenant_registry")
        tenant_id = _g().get("runtime_tenant_id", "default")
        if registry is None:
            raise HTTPException(503, "账号组注册表尚未初始化")
        conn = _g()["conn"]
        asset = conn.execute(
            "SELECT stored_filename, status, review_status FROM media_assets WHERE id = ?", (asset_id,),
        ).fetchone()
        if asset is None or asset["status"] != "active":
            raise HTTPException(404, "图片素材不存在或已归档")
        if asset["review_status"] != "approved":
            raise HTTPException(409, "该素材尚未通过人工审核，不能用于内容")
        content = conn.execute(
            "SELECT id, round_id, platform, title FROM contents WHERE id = ?",
            (req.content_id,),
        ).fetchone()
        if content is None:
            raise HTTPException(404, "目标内容不存在")
        draft = conn.execute(
            "SELECT status FROM drafts WHERE content_id = ? AND platform = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (req.content_id, content["platform"]),
        ).fetchone()
        if draft is None or draft["status"] not in ("draft_pending", "draft_saved"):
            raise HTTPException(409, "只能为尚未发布的待存草稿或平台草稿选择图片")
        paths = registry.paths(tenant_id)
        image_path = (paths.media_dir / asset["stored_filename"]).resolve()
        if not image_path.is_relative_to(paths.media_dir.resolve()) or not image_path.is_file():
            raise HTTPException(404, "素材文件不存在")
        conn.execute(
            "UPDATE contents SET image_paths = ?, visual_status = 'media_asset', "
            "visual_reason = NULL WHERE id = ?",
            (json.dumps([str(image_path)], ensure_ascii=False), req.content_id),
        )
        conn.execute(
            "INSERT INTO media_asset_usages (media_asset_id, content_id, platform, usage_role) "
            "VALUES (?, ?, ?, ?)",
            (asset_id, req.content_id, content["platform"], req.usage_role),
        )
        conn.commit()
        return {
            "asset_id": asset_id, "content_id": req.content_id,
            "platform": content["platform"], "image_assigned": True,
            "message": "已将素材设为该内容的图片。若平台草稿此前已保存，需重新同步平台草稿后再发布。",
        }

    @app.post("/api/media/{asset_id}/archive")
    def archive_media_asset(asset_id: int):
        conn = _g()["conn"]
        row = conn.execute("SELECT id, status FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "图片素材不存在")
        conn.execute(
            "UPDATE media_assets SET status = 'archived', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (asset_id,),
        )
        conn.commit()
        return {"id": asset_id, "status": "archived", "already_archived": row["status"] == "archived"}

    @app.post("/api/tenant-backup")
    def create_tenant_backup():
        cfg = _g()["cfg"]
        tenant_id = _g().get("runtime_tenant_id", "default")
        data_dir = Path(cfg.resolve_path(cfg.paths.data_dir))
        db_path = Path(cfg.resolve_path(cfg.paths.db_file))
        backup_dir = Path(cfg.resolve_path(cfg.paths.backup_dir))
        try:
            from src.core.tenant_backup import create_backup
            target = create_backup(db_path, data_dir, backup_dir, tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("账号组备份失败 tenant=%s", tenant_id)
            raise HTTPException(500, f"账号组备份失败：{exc}") from exc
        return {
            "backup_name": target.name,
            "size_bytes": target.stat().st_size,
            "created_at": datetime.fromtimestamp(target.stat().st_mtime).isoformat(timespec="seconds"),
            "includes": ["SQLite 数据库快照", "活动图片素材", "账号组运行配置覆盖"],
            "excludes": ["登录态 Cookie", "浏览器登录 profile", "API 密钥", "共享原始热点数据"],
        }

    @app.get("/api/tenant-backups")
    def list_tenant_backups():
        cfg = _g()["cfg"]
        tenant_id = _g().get("runtime_tenant_id", "default")
        backup_dir = Path(cfg.resolve_path(cfg.paths.backup_dir))
        items = []
        for path in sorted(backup_dir.glob(f"iterwave_{tenant_id}_*.zip"), reverse=True):
            if not path.is_file():
                continue
            items.append({
                "backup_name": path.name,
                "size_bytes": path.stat().st_size,
                "created_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            })
        return {"items": items}

    @app.post("/api/tenant-backup/restore")
    def stage_tenant_backup_restore(req: TenantBackupRestoreRequest):
        if not req.confirm:
            raise HTTPException(400, "恢复操作需要显式确认")
        if _running_rounds:
            raise HTTPException(409, "当前仍有轮次在执行，不能安排恢复")
        cfg = _g()["cfg"]
        tenant_id = _g().get("runtime_tenant_id", "default")
        data_dir = Path(cfg.resolve_path(cfg.paths.data_dir))
        backup_dir = Path(cfg.resolve_path(cfg.paths.backup_dir)).resolve()
        backup_name = Path(req.backup_name).name
        if backup_name != req.backup_name or not backup_name.startswith(f"iterwave_{tenant_id}_") or not backup_name.endswith(".zip"):
            raise HTTPException(422, "备份名称无效或不属于当前账号组")
        target = (backup_dir / backup_name).resolve()
        if not target.is_relative_to(backup_dir) or not target.is_file():
            raise HTTPException(404, "备份文件不存在")
        pending = data_dir / ".pending_restore.json"
        pending.write_text(
            json.dumps({"tenant_id": tenant_id, "backup_name": backup_name}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            pending.chmod(0o600)
        except OSError:
            pass
        return {
            "staged": True,
            "restart_required": True,
            "backup_name": backup_name,
            "message": "恢复将在后端下次启动、打开数据库前执行。请停止使用当前实例并立即重启；重启前的新写入不会保留。",
        }

    @app.get("/api/tenants")
    def list_tenants():
        registry = _g().get("tenant_registry")
        if registry is None:
            raise HTTPException(503, "账号组注册表尚未初始化")
        try:
            return {
                "runtime_tenant_id": _g().get("runtime_tenant_id", "default"),
                "selected_tenant_id": registry.active_id(),
                "restart_required": registry.active_id() != _g().get("runtime_tenant_id", "default"),
                "items": registry.list(),
            }
        except TenantError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/tenants")
    def create_tenant(req: TenantCreateRequest):
        registry = _g().get("tenant_registry")
        if registry is None:
            raise HTTPException(503, "账号组注册表尚未初始化")
        try:
            paths = registry.create(req.tenant_id, req.name)
        except TenantError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "tenant": {"id": req.tenant_id, "name": req.name.strip()},
            "paths": paths.as_dict(),
            "restart_required": False,
        }

    @app.post("/api/tenants/select")
    def select_tenant(req: TenantSelectRequest):
        registry = _g().get("tenant_registry")
        if registry is None:
            raise HTTPException(503, "账号组注册表尚未初始化")
        if _running_rounds:
            raise HTTPException(409, "当前仍有轮次在执行，不能切换账号组")
        try:
            selected = registry.select_active(req.tenant_id)
        except TenantError as exc:
            raise HTTPException(404, str(exc)) from exc
        runtime_id = _g().get("runtime_tenant_id", "default")
        return {
            "selected": selected,
            "runtime_tenant_id": runtime_id,
            "restart_required": selected["id"] != runtime_id,
            "message": (
                "账号组已选择。当前进程仍使用旧账号组；请重启服务后切换才会生效。"
                if selected["id"] != runtime_id
                else "当前账号组已是运行中的账号组。"
            ),
        }

    @app.get("/api/global-monitor")
    def global_monitor(days: int = Query(30, ge=1, le=365)):
        """只读跨账号组汇总。每个数据库独立打开，缺表/未初始化数据库按不可用处理，不影响其他账号组。"""
        registry = _g().get("tenant_registry")
        if registry is None:
            raise HTTPException(503, "账号组注册表尚未初始化")
        from datetime import timedelta
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        tenant_rows = []
        totals = {"tenants": 0, "available_tenants": 0, "contents_period": 0, "contents_all": 0, "drafts_all": 0, "drafts_pending": 0, "drafts_saved": 0, "drafts_published": 0, "drafts_failed": 0, "media_active": 0, "media_usage": 0}
        platform_totals = {p: {"contents": 0, "drafts": 0} for p in ("xhs", "douyin", "kuaishou", "wechat_channels")}
        for tenant in registry.list():
            tenant_id = tenant["id"]
            paths = registry.paths(tenant_id, name=str(tenant.get("name") or tenant_id))
            row = {"tenant_id": tenant_id, "tenant_name": tenant["name"], "created_at": tenant.get("created_at"), "db_available": False, "contents_period": 0, "contents_all": 0, "drafts_all": 0, "drafts_pending": 0, "drafts_saved": 0, "drafts_published": 0, "drafts_failed": 0, "media_active": 0, "media_usage": 0, "platforms": {}}
            db_file = paths.db_file
            if db_file.exists():
                conn = None
                try:
                    conn = sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True, timeout=2)
                    conn.row_factory = sqlite3.Row
                    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
                    if "contents" in tables:
                        row["contents_all"] = int(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0])
                        cols = {r["name"] for r in conn.execute("PRAGMA table_info(contents)").fetchall()}
                        if "created_at" in cols:
                            row["contents_period"] = int(conn.execute("SELECT COUNT(*) FROM contents WHERE datetime(created_at) >= datetime(?)", (since,)).fetchone()[0])
                            for item in conn.execute("SELECT platform, COUNT(*) AS n FROM contents WHERE datetime(created_at) >= datetime(?) GROUP BY platform", (since,)).fetchall():
                                platform = item["platform"]
                                row["platforms"].setdefault(platform, {"contents": 0, "contents_period": 0, "drafts": 0})["contents_period"] = int(item["n"])
                        for item in conn.execute("SELECT platform, COUNT(*) AS n FROM contents GROUP BY platform").fetchall():
                            platform = item["platform"]
                            row["platforms"].setdefault(platform, {"contents": 0, "contents_period": 0, "drafts": 0})["contents"] = int(item["n"])
                            if platform in platform_totals: platform_totals[platform]["contents"] += int(item["n"])
                    if "drafts" in tables:
                        row["drafts_all"] = int(conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0])
                        for item in conn.execute("SELECT status, COUNT(*) AS n FROM drafts GROUP BY status").fetchall():
                            status, count = str(item["status"]), int(item["n"])
                            if status in ("draft_pending", "pending", "pending_review"): row["drafts_pending"] += count
                            elif status == "draft_saved": row["drafts_saved"] += count
                            elif status in ("user_published", "data_fetching", "published"): row["drafts_published"] += count
                            elif status in ("failed", "error"): row["drafts_failed"] += count
                        for item in conn.execute("SELECT platform, COUNT(*) AS n FROM drafts GROUP BY platform").fetchall():
                            platform = item["platform"]
                            row["platforms"].setdefault(platform, {"contents": 0, "contents_period": 0, "drafts": 0})["drafts"] = int(item["n"])
                            if platform in platform_totals: platform_totals[platform]["drafts"] += int(item["n"])
                    if "media_assets" in tables:
                        row["media_active"] = int(conn.execute("SELECT COUNT(*) FROM media_assets WHERE status = 'active'").fetchone()[0])
                    if "media_asset_usages" in tables:
                        row["media_usage"] = int(conn.execute("SELECT COUNT(*) FROM media_asset_usages").fetchone()[0])
                    row["db_available"] = True
                except (sqlite3.Error, OSError) as exc:
                    logger.warning("global monitor skipped tenant=%s: %s", tenant_id, exc)
                finally:
                    if conn is not None: conn.close()
            totals["tenants"] += 1
            if row["db_available"]: totals["available_tenants"] += 1
            for key in ("contents_period", "contents_all", "drafts_all", "drafts_pending", "drafts_saved", "drafts_published", "drafts_failed", "media_active", "media_usage"): totals[key] += row[key]
            tenant_rows.append(row)
        return {"days": days, "since": since, "totals": totals, "platform_totals": platform_totals, "tenants": tenant_rows, "account_count_note": "当前数据模型未保存可去重的平台账号身份；此处不将平台数或快照数冒充账号数。"}

    # --- 访问保护（口令 / 会话 / 脚本令牌）---
    @app.get("/api/auth/status")
    def auth_status(request: Request):
        # 状态接口必须与中间件和首次设密接口使用同一信任判定；
        # 否则未显式启用本机初始化旁路时，前端会误判已认证并跳过设置页。
        local_direct = auth.is_trusted_local_setup(request)

        ip = request.client.host if request.client else "unknown"
        st = auth.status(request.cookies.get(auth.COOKIE_NAME), request.headers, ip=ip)
        if local_direct and not auth.password_configured():
            st["configured"] = True
            st["authenticated"] = True
            st["needs_login"] = False
        return st

    @app.post("/api/auth/login")
    def auth_login(payload: dict, request: Request, response: Response):
        password = str(payload.get("password") or "")
        ip = request.client.host if request.client else "unknown"
        try:
            token = auth.login(password, ip)
        except PermissionError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        response.headers["set-cookie"] = auth.session_cookie_header(token, secure=request.url.scheme == "https")
        return auth.status(token, request.headers)

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        auth.logout_session(request.cookies.get(auth.COOKIE_NAME))
        response.headers["set-cookie"] = auth.clear_cookie_header()
        return {"authenticated": False, "needs_login": auth.password_configured()}

    @app.post("/api/auth/password")
    def auth_set_password(payload: dict, request: Request):
        # 首次设置前仅初始化路由可用；仍需防止公网访客抢先设置口令并锁定管理员。
        # 本机直连允许首次设置；公网域名/反向代理部署必须通过服务端环境变量提供的一次性引导口令。
        if not auth.password_configured():
            local_direct = auth.is_trusted_local_setup(request)
            bootstrap = os.getenv("ITERWAVE_BOOTSTRAP_TOKEN", "")
            provided = str((payload or {}).get("bootstrap_token") or "")
            if not local_direct:
                client_ip = request.client.host if request.client else "unknown"
                if auth.is_rate_limited(client_ip):
                    raise HTTPException(status_code=429, detail="引导口令尝试次数过多，请 10 分钟后再试")
                valid_bootstrap = bool(bootstrap and provided) and hmac.compare_digest(provided, bootstrap)
                if not valid_bootstrap:
                    auth.record_failure(client_ip)
                    raise HTTPException(
                        status_code=403,
                        detail="首次设置口令需本机访问，或先在服务器配置 ITERWAVE_BOOTSTRAP_TOKEN 后提交引导口令",
                    )
                auth.clear_failures(client_ip)
        try:
            auth.set_password(str((payload or {}).get("password") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"configured": True, "needs_login": True,
                "message": "访问口令已更新，请用新口令登录"}

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
            item["achievement_rate"] = GoalProgressUpdater.rate_for(item)
            result.append(item)
        return result

    @app.get("/api/goals/summary")
    def goals_summary():
        """总体目标进度（goal-hero）：等权平均口径由后端统一计算，前端只展示。"""
        conn = _g()["conn"]
        rows = conn.execute("SELECT * FROM goals").fetchall()
        return GoalProgressUpdater.summarize(rows, mode=_g()["cfg"].ultimate_goal.goal_mode)

    # --- 账号级快照（涨粉类目标的数据来源）---
    @app.get("/api/account_snapshots")
    def list_account_snapshots(platform: Optional[str] = Query(None), limit: int = Query(30, ge=1, le=500)):
        conn = _g()["conn"]
        sql = "SELECT * FROM account_snapshots"
        params: list = []
        if platform:
            sql += " WHERE platform = ?"
            params.append(platform)
        sql += " ORDER BY captured_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    @app.post("/api/account_snapshots")
    def add_account_snapshot(req: dict):
        """手动录入某平台当前粉丝 / 获赞总数（作品级读回拿不到新增粉丝）。录入后立即刷新目标进度。"""
        conn = _g()["conn"]
        platform = str((req or {}).get("platform") or "")
        if platform not in ("xhs", "douyin", "kuaishou", "wechat_channels"):
            raise HTTPException(422, "platform 必须是 xhs / douyin / kuaishou / wechat_channels")

        def _int(name: str) -> Optional[int]:
            v = (req or {}).get(name)
            if v is None or v == "":
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise HTTPException(422, f"{name} 必须是非负数字")
            return int(v)

        followers, likes_total = _int("followers"), _int("likes_total")
        if followers is None and likes_total is None:
            raise HTTPException(422, "粉丝数和获赞数至少填一个")
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO account_snapshots (platform, followers, likes_total, captured_at, source) "
            "VALUES (?, ?, ?, ?, 'manual')",
            (platform, followers, likes_total, now),
        )
        conn.commit()
        GoalProgressUpdater(_g()["cfg"], conn).update_all()
        return {"platform": platform, "followers": followers, "likes_total": likes_total, "captured_at": now}

    @app.post("/api/goals/{goal_id}/manual_value")
    def set_goal_manual_value(goal_id: str, req: dict):
        """数据来源为「人工录入」的目标：由人填写当前值（自动刷新不会覆盖）。"""
        conn = _g()["conn"]
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if not row:
            raise HTTPException(404, "目标不存在")
        if (row["data_source"] or "") != "manual":
            raise HTTPException(409, "只有数据来源为「人工录入」的目标可以手动填写当前值")
        v = (req or {}).get("current_value")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(422, "current_value 必须是数字")
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE goals SET current_value = ?, progress_updated_at = ? WHERE id = ?",
                     (float(v), now, goal_id))
        conn.commit()
        updater = GoalProgressUpdater(_g()["cfg"], conn)
        updater.update_goal(conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())
        item = dict(conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())
        item["achievement_rate"] = GoalProgressUpdater.rate_for(item)
        return item

    # --- 账号画像（全局 + 平台）与目标模式 ---
    @app.get("/api/personas")
    def get_personas():
        from ..core.persona import personas_view
        return personas_view(_g()["cfg"])

    @app.post("/api/goals")
    def create_goal(req: CreateGoalRequest):
        """创建阶段目标（R21：6 个口径字段齐备才落库）。

        注意：这里是 goals 表，不是 spiral_rounds。创建目标 ≠ 启动轮次，
        轮次统一走 POST /api/rounds。
        """
        conn = _g()["conn"]
        goal_id = f"goal-{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat(timespec="seconds")
        if req.parent_goal_id:
            parent = conn.execute("SELECT goal_level FROM goals WHERE id = ?", (req.parent_goal_id,)).fetchone()
            if not parent:
                raise HTTPException(422, "所属总目标不存在")
            if (parent["goal_level"] or "platform") != "north_star":
                raise HTTPException(422, "只能挂到总目标下面")
            if req.goal_level == "north_star":
                raise HTTPException(422, "总目标不能再挂到其它总目标下")
        conn.execute(
            "INSERT INTO goals (id, goal_type, goal_text, target_metric, target_value,"
            " target_platform, metric_scope, baseline_value, baseline_at,"
            " metric_period_start, metric_period_end, data_source, status, created_at,"
            " goal_level, parent_goal_id, weight, contribution_note)"
            " VALUES (?, 'stage', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                goal_id, req.goal_text, req.target_metric, req.target_value,
                (None if req.goal_level == "north_star" else (req.target_platform or None)),
                req.metric_scope, req.baseline_value,
                req.baseline_at, req.metric_period_start, req.metric_period_end,
                req.data_source, now,
                req.goal_level, req.parent_goal_id, req.weight, req.contribution_note,
            ),
        )
        conn.commit()
        item = dict(conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())
        item["achievement_rate"] = GoalProgressUpdater.rate_for(item)
        return item

    @app.patch("/api/goals/{goal_id}")
    def update_goal(goal_id: str, req: "GoalUpdateRequest"):
        """编辑阶段目标口径 / 暂停或完成目标（current_value 只由采集回写）。"""
        conn = _g()["conn"]
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if not row:
            raise HTTPException(404, "目标不存在")
        fields = req.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(422, "没有需要更新的字段")
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE goals SET {sets} WHERE id = ?", (*fields.values(), goal_id))
        conn.commit()
        item = dict(conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())
        item["achievement_rate"] = GoalProgressUpdater.rate_for(item)
        return item

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
            item["achievement_rate"] = GoalProgressUpdater.rate_for(item)
            result.append(item)
        return {"updated": values, "goals": result, "summary": GoalProgressUpdater.summarize(rows, mode=_g()["cfg"].ultimate_goal.goal_mode)}

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
            "SELECT d.*, c.review_rejected AS review_rejected, "
            "c.review_rejection_reason AS review_rejection_reason "
            "FROM drafts d JOIN contents c ON c.id = d.content_id WHERE c.round_id = ?",
            (round_id,),
        ).fetchall()
        result = dict(row)
        round_drafts = []
        for draft in drafts:
            item = dict(draft)
            item["review_rejected"] = bool(item.get("review_rejected"))
            round_drafts.append(item)
        result["drafts"] = round_drafts
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

    @app.post("/api/rounds/{round_id}/save_pending")
    async def save_pending_drafts(round_id: int):
        """恢复已生成但尚未存到平台的草稿，不重新生成内容、不执行发布操作。"""
        if round_id in _running_rounds:
            raise HTTPException(
                409, f"轮次 {round_id} 正在执行中，请等待本次执行结束"
            )
        _running_rounds.add(round_id)
        try:
            return await _g()["executor"].save_pending_drafts(round_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            _running_rounds.discard(round_id)

    @app.post("/api/rounds/{round_id}/transition")
    def round_transition(round_id: int, req: TransitionRequest):
        conn = _g()["conn"]
        sm_round = _g()["sm_round"]
        ok, msg = sm_round.can_transition(round_id, req.target_status)
        if not ok:
            raise HTTPException(400, msg)

        current_row = conn.execute(
            "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
        ).fetchone()
        if current_row and current_row["status"] != req.target_status:
            # 正常状态只能由对应业务事件推进：运行执行器、用户发布确认、
            # 数据采集与复盘任务。通用状态接口只提供安全取消，不允许跳过业务校验。
            if req.target_status != "cancelled":
                raise HTTPException(
                    409,
                    "该状态由业务流程自动推进，不支持手工修改。请使用运行本轮、发布确认或等待系统采集与复盘",
                )

        if req.target_status == "cancelled":
            current = conn.execute(
                "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
            ).fetchone()
            # 状态机同状态幂等；重复取消不再跑副作用。
            if current and current["status"] != "cancelled":
                # 不能在异步生产仍运行时取消：执行器可能继续调用平台存草稿，
                # 但轮次已是终态，后续的发布/复盘会失去状态入口。
                if round_id in _running_rounds:
                    raise HTTPException(
                        409,
                        "本轮仍在执行，不能中途取消。请等待本次执行结束后再处理草稿",
                    )

                publish_history = conn.execute(
                    "SELECT COUNT(*) FROM drafts d JOIN contents c ON c.id = d.content_id "
                    "WHERE c.round_id = ? AND (d.user_confirmed_at IS NOT NULL "
                    "OR d.status IN ('user_published', 'data_fetching', 'published'))",
                    (round_id,),
                ).fetchone()[0]
                if publish_history:
                    raise HTTPException(
                        409,
                        f"本轮有 {publish_history} 篇已确认发布或存在发布历史的内容，"
                        "取消轮次会中断数据采集与复盘。请让采集和复盘流程正常收口",
                    )

                saved_count = conn.execute(
                    "SELECT COUNT(*) FROM drafts d JOIN contents c ON c.id = d.content_id "
                    "WHERE c.round_id = ? AND d.status = 'draft_saved'",
                    (round_id,),
                ).fetchone()[0]
                if saved_count:
                    raise HTTPException(
                        409,
                        f"本轮仍有 {saved_count} 篇已存入平台的草稿。请先在本系统逐篇取消这些草稿，"
                        "并按提示到平台端手动清理；确认没有可发布草稿后再取消轮次",
                    )

                # 未执行平台副作用的本地 pending 草稿随轮次一并取消，避免留下
                # cancelled 轮次里仍可恢复存平台的 draft_pending 记录。
                pending = conn.execute(
                    "SELECT d.id FROM drafts d JOIN contents c ON c.id = d.content_id "
                    "WHERE c.round_id = ? AND d.status = 'draft_pending'",
                    (round_id,),
                ).fetchall()
                for item in pending:
                    if not _g()["sm_draft"].transition(
                        item["id"], "cancelled", reason="所属轮次已取消",
                    ):
                        raise HTTPException(
                            409, f"草稿 {item['id']} 未能取消，轮次状态未变更",
                        )

        sm_round.transition(round_id, req.target_status, reason=req.reason)
        return {"round_id": round_id, "new_status": req.target_status}

    # --- 草稿 ---
    @app.get("/api/drafts")
    def list_drafts(
        status: Optional[str] = Query(None),
        platform: Optional[str] = Query(None),
    ):
        conn = _g()["conn"]
        sql = (
            "SELECT d.*, c.title, c.body, c.tags, c.image_paths, c.round_id, c.topic_id, "
            "c.visual_status AS visual_status, c.visual_reason AS visual_reason, "
            "c.platform as cfg_platform, c.guard_outcome AS guard_outcome, c.guard_reason AS guard_reason, "
            "c.review_rejected AS review_rejected, "
            "c.review_rejection_reason AS review_rejection_reason "
            "FROM drafts d LEFT JOIN contents c ON d.content_id = c.id"
        )
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
        from src.core.content_pipeline import _llm_available
        llm_available = _llm_available(_g()["cfg"])
        items = []
        from src.core.content_version import content_version_hash, parse_json_list
        for item in rows:
            result = dict(item)
            outcome = result.get("guard_outcome")
            result["review_rejected"] = bool(result.get("review_rejected"))
            result["guard_confirmation_allowed"] = (
                not result["review_rejected"]
                and (outcome == "pass" or (outcome == "warn" and not llm_available))
            )
            if result.get("status") == "draft_pending":
                result["platform_content_sync"] = "local_only"
            elif result.get("status") == "draft_saved":
                tags = parse_json_list(result.get("tags"))
                stored_images = parse_json_list(result.get("image_paths"))
                payload_images = [str(stored_images[0])] if stored_images and stored_images[0] else []
                current_hash = content_version_hash(
                    result.get("title"), result.get("body"), tags, payload_images,
                )
                saved_hash = result.get("saved_content_hash")
                result["platform_content_sync"] = (
                    "unknown" if not saved_hash
                    else "matched" if saved_hash == current_hash
                    else "stale"
                )
            else:
                result["platform_content_sync"] = None
            # 上面几个字段只用于后端计算版本状态，不属于草稿列表的响应契约；
            # 尤其 image_paths 可能是本机绝对路径，不能随便随列表 API 下发。
            for internal_field in (
                "body", "tags", "image_paths", "saved_content_hash", "saved_platform_title",
            ):
                result.pop(internal_field, None)
            items.append(result)
        return items

    @app.post("/api/drafts/{draft_id}/publish_confirm")
    def confirm_publish(draft_id: str, req: Optional[PublishConfirmRequest] = None):
        """R1 用户手动点击"✅ 我已发布"触发 user_published。

        req.note_url 可选：平台笔记链接。被动评论轮询靠它定位笔记，
        不填则该草稿跳过评论轮询（内部 draft id 拼不出有效笔记地址）。
        """
        conn = _g()["conn"]
        row = conn.execute(
            "SELECT d.platform, d.status, d.error_msg, d.saved_content_hash, "
            "c.id AS content_id, c.round_id, c.title, c.body, c.tags, c.image_paths, "
            "c.guard_outcome, c.guard_reason, c.review_rejected, c.review_rejection_reason "
            "FROM drafts d JOIN contents c ON c.id = d.content_id WHERE d.id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"草稿 {draft_id} 不存在")

        note_url = (req.note_url or "").strip() if req else ""
        if note_url and not note_url.startswith(("http://", "https://")):
            raise HTTPException(422, "note_url 必须是完整的 http(s) 链接")

        # 重复确认应幂等：不重新注册采集任务，不重复推进轮次；已发布记录保留原状态。
        if row["status"] in ("user_published", "data_fetching", "published"):
            if note_url:
                conn.execute(
                    "UPDATE drafts SET platform_note_url = COALESCE(platform_note_url, ?) WHERE id = ?",
                    (note_url, draft_id),
                )
                conn.commit()
            current_url = conn.execute(
                "SELECT platform_note_url FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            return {
                "draft_id": draft_id,
                "status": row["status"],
                "note_url": current_url["platform_note_url"] if current_url else None,
                "already_confirmed": True,
            }

        if row["status"] not in ("draft_saved", "draft_pending"):
            raise HTTPException(
                409,
                f"草稿当前状态为 {row['status']}，不能确认发布；仅待存内容和平台草稿可确认",
            )

        # 发布确认记录的是用户已经在外部平台完成的动作：正常路径须有当前质检结果；
        # 若发布事实上已发生但质检失效/失败，允许显式登记原因以保留真实发布与采集闭环。
        from src.core.content_pipeline import _llm_available
        guard_outcome = row["guard_outcome"]
        llm_available = _llm_available(_g()["cfg"])
        guard_accepted = guard_outcome == "pass" or (
            guard_outcome == "warn" and not llm_available
        )
        review_rejected = bool(row["review_rejected"])
        review_rejection_reason = row["review_rejection_reason"] or ""
        override_reason = (req.quality_override_reason or "").strip() if req else ""
        requires_override = (not guard_accepted) or review_rejected
        if requires_override and not override_reason:
            blockers = []
            if review_rejected:
                blockers.append(
                    f"该内容已被人工驳回：{review_rejection_reason or '未提供驳回原因'}"
                )
            if not guard_accepted:
                blockers.append(
                    f"当前质检状态为 {guard_outcome or '未检查'}："
                    f"{row['guard_reason'] or '当前文案尚未通过目标检查'}"
                )
            raise HTTPException(
                409,
                "；".join(blockers)
                + "。未发布时请先修改并重新提交审核；如果已在平台真实发布，"
                "需填写例外原因才能登记真实发布与数据采集",
            )

        # 已存平台草稿的真实载荷必须与当前本地版本一致；旧记录无指纹按 unknown 处理。
        # 若用户尚未发布，应先在平台同步草稿；若已发布旧版，则需要单独说明例外原因。
        platform_sync_state = None
        platform_sync_override_reason = (
            (req.platform_sync_override_reason or "").strip() if req else ""
        )
        current_hash = None
        if row["status"] == "draft_saved":
            from src.core.content_version import content_version_hash, parse_json_list
            stored_images = parse_json_list(row["image_paths"])
            payload_images = [str(stored_images[0])] if stored_images and stored_images[0] else []
            current_hash = content_version_hash(
                row["title"], row["body"], parse_json_list(row["tags"]), payload_images,
            )
            saved_hash = row["saved_content_hash"]
            platform_sync_state = (
                "unknown" if not saved_hash
                else "matched" if saved_hash == current_hash
                else "stale"
            )
            if platform_sync_state != "matched" and not platform_sync_override_reason:
                sync_desc = (
                    "历史平台草稿没有保存版本指纹，无法证明平台稿与本地稿一致"
                    if platform_sync_state == "unknown"
                    else "本地文案已不同于上次存入平台的版本"
                )
                raise HTTPException(
                    409,
                    f"{sync_desc}。尚未发布时，请先在平台端更新草稿并点击「确认平台版本已同步」；"
                    "如果实际发布的就是平台旧版本，请填写平台版本例外原因后再登记",
                )

        sched = _g().get("scheduler")
        if sched is not None and getattr(sched, "conn", None) is not None and getattr(sched, "sm_draft", None) is not None:
            # 统一入口：改状态 + 回写链接 + 注册发布后数据采集 + 推进轮次（与轮询识别完全一致）
            ok = sched.on_user_published(draft_id, via="用户确认发布", note_url=note_url or None)
            if not ok:
                raise HTTPException(400, "状态迁移失败")
            if note_url:
                conn.execute("UPDATE drafts SET platform_note_url = ? WHERE id = ?", (note_url, draft_id))
                conn.commit()
        else:
            ok = _g()["sm_draft"].transition(draft_id, "user_published", reason="用户确认发布")
            if not ok:
                raise HTTPException(400, "状态迁移失败")
            if note_url:
                conn.execute(
                    "UPDATE drafts SET platform_note_url = ? WHERE id = ?",
                    (note_url, draft_id),
                )
                conn.commit()
            # 首篇 user_published → collecting_metrics
            round_id = _find_round_for_draft(conn, draft_id)
            if round_id:
                _g()["sm_round"].on_first_publish_confirmed(round_id)
        # 质检例外只用于登记已发生的外部发布；保留理由，方便复盘追溯。
        quality_override_recorded = False
        if requires_override and override_reason:
            try:
                conn.execute(
                    "INSERT INTO user_override (round_id, override_field, original_value, "
                    "override_value, reason, overridden_by, overridden_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["round_id"],
                        "publish_review_rejection_override" if review_rejected else "publish_quality_override",
                        json.dumps({
                            "review_rejected": review_rejected,
                            "review_rejection_reason": review_rejection_reason,
                            "guard_outcome": guard_outcome,
                            "guard_reason": row["guard_reason"],
                        }, ensure_ascii=False)[:500],
                        json.dumps({"status": "user_published"}, ensure_ascii=False),
                        override_reason, "publish-confirm-ui",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                conn.commit()
                quality_override_recorded = True
            except Exception:  # noqa: BLE001 — 采集链路优先，审计写失败留日志
                logger.exception("发布质检例外审计写入失败 draft=%s", draft_id)

        platform_sync_override_recorded = False
        if platform_sync_state and platform_sync_state != "matched" and platform_sync_override_reason:
            try:
                conn.execute(
                    "INSERT INTO user_override (round_id, override_field, original_value, "
                    "override_value, reason, overridden_by, overridden_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["round_id"], "platform_content_sync_override",
                        json.dumps({
                            "state": platform_sync_state,
                            "saved_content_hash": row["saved_content_hash"],
                            "current_content_hash": current_hash,
                        }, ensure_ascii=False)[:500],
                        json.dumps({"status": "user_published"}, ensure_ascii=False),
                        platform_sync_override_reason, "publish-confirm-ui",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                conn.commit()
                platform_sync_override_recorded = True
            except Exception:  # noqa: BLE001 — 已发生的外部发布不能因审计写失败丢失采集
                logger.exception("平台内容版本例外审计写入失败 draft=%s", draft_id)

        # G-15 场景：发布确认留痕（用户手动确认，非系统自动发布）
        message = f"草稿 {draft_id} 已由用户确认发布，进入数据采集"
        if quality_override_recorded:
            message += f"（质检/审核例外登记：{override_reason[:80]}）"
        if platform_sync_override_recorded:
            message += f"（平台内容版本例外登记：{platform_sync_override_reason[:80]}）"
        _g()["notifier"].emit(
            "publish_confirmed",
            message,
            platform=dict(row)["platform"] if row else None,
        )
        return {"draft_id": draft_id, "status": "user_published", "note_url": note_url or None}

    @app.post("/api/drafts/{draft_id}/confirm_platform_sync")
    def confirm_platform_draft_sync(
        draft_id: str, req: PlatformSyncConfirmRequest,
    ):
        """用户确认已在平台端手动更新草稿，使其与当前本地文案版本一致。"""
        conn = _g()["conn"]
        row = conn.execute(
            "SELECT d.status, d.saved_content_hash, d.saved_platform_title, "
            "c.id AS content_id, c.round_id, c.title, c.body, c.tags, c.image_paths, "
            "c.guard_outcome, c.guard_reason, "
            "c.review_rejected, c.review_rejection_reason "
            "FROM drafts d JOIN contents c ON c.id = d.content_id WHERE d.id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"草稿 {draft_id} 不存在")
        if row["status"] != "draft_saved":
            raise HTTPException(
                409, f"当前状态为 {row['status']}，只有已存入平台的草稿可以确认同步",
            )
        _ensure_content_round_idle(row, "确认平台版本同步")
        if bool(row["review_rejected"]):
            raise HTTPException(
                409,
                f"该内容已被人工驳回：{row['review_rejection_reason'] or '未提供原因'}。"
                "请先修改文案、重新过目标检查并提交审核",
            )
        from src.core.content_pipeline import _llm_available
        llm_available = _llm_available(_g()["cfg"])
        guard_outcome = row["guard_outcome"]
        guard_accepted = guard_outcome == "pass" or (
            guard_outcome == "warn" and not llm_available
        )
        if not guard_accepted:
            raise HTTPException(
                409,
                f"当前质检状态为 {guard_outcome or '未检查'}："
                f"{row['guard_reason'] or '当前文案尚未通过目标检查'}。"
                "请先运行「重新过目标检查」并通过后，再确认平台版本同步",
            )

        from src.core.content_version import content_version_hash, parse_json_list
        stored_images = parse_json_list(row["image_paths"])
        payload_images = [str(stored_images[0])] if stored_images and stored_images[0] else []
        current_hash = content_version_hash(
            row["title"], row["body"], parse_json_list(row["tags"]), payload_images,
        )
        old_hash = row["saved_content_hash"]
        if old_hash == current_hash:
            return {
                "draft_id": draft_id,
                "platform_content_sync": "matched",
                "already_synced": True,
                "message": "平台草稿版本已与当前本地文案一致，无需重复登记",
            }
        conn.execute(
            "UPDATE drafts SET saved_content_hash = ?, saved_platform_title = ? WHERE id = ?",
            (current_hash, row["title"], draft_id),
        )
        conn.execute(
            "INSERT INTO user_override (round_id, override_field, original_value, "
            "override_value, reason, overridden_by, overridden_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["round_id"], "platform_content_sync_confirmed",
                json.dumps({"saved_content_hash": old_hash}, ensure_ascii=False)[:500],
                json.dumps({"saved_content_hash": current_hash}, ensure_ascii=False)[:500],
                req.reason.strip(), "publish-confirm-ui",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        return {
            "draft_id": draft_id,
            "platform_content_sync": "matched",
            "message": "已记录平台草稿与当前本地文案版本一致",
        }

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
            conf = (plan.get("per_platform") or {}).get(p.value, {})
            per_platform[p.value] = {
                "enabled": p.value in enabled,
                "config_enabled": bool(getattr(cfg.platforms, p.value).enabled),
                "topic_count": PlanVersionManager.platform_topic_count(plan, p.value),
                "topic_count_override": conf.get("topic_count"),
                "publish_slots": conf.get("publish_slots") or [],
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
            per_platform=req.per_platform,
            cadence=req.cadence,
        )
        alloc_total = sum(req.dimension_allocation.values())
        if alloc_total > req.topic_count_per_platform:
            raise HTTPException(400, f"维度分配合计 {alloc_total} 条，超过每平台选题数 {req.topic_count_per_platform}")
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
            per_platform=plan.get("per_platform"),
            cadence=plan.get("cadence"),
        )
        if sum(allocation.values()) > new_plan["topic_count_per_platform"]:
            new_plan["topic_count_per_platform"] = min(50, sum(allocation.values()))
        result = pvm.save_plan(new_plan, author="web-ui:adopt-gaps")
        result.update({
            "previous_signature": previous_signature,
            "adopted_dimensions": dims,
            "dimension_allocation": allocation,
        })
        return result

    @app.post("/api/plans/suggest_from_goal")
    def plan_suggest_from_goal(req: dict):
        """阶段目标拆分（§22 ⑥）：目标 → 每平台篇数 × 计划轮数 × 轮间隔（只给建议，不保存）。"""
        from src.core.plan_version import suggest_plan_from_goal
        goal_id = str((req or {}).get("goal_id") or "").strip()
        if not goal_id:
            raise HTTPException(400, "goal_id 不能为空")
        plan, *_ = _g()["plan_version_manager"].load_plan()
        try:
            return suggest_plan_from_goal(_g()["cfg"], _g()["conn"], goal_id, plan)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    # --- 选题池（本地选题库 + 热点转入 + 手动录入；软删除可恢复）---
    @app.get("/api/topics")
    def list_topic_pool(
        q: str = "", dimension: str = "", source: str = Query("", pattern="^(|kb|hot|manual)$"),
        status: str = Query("active", pattern="^(active|excluded|all)$"),
        limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
    ):
        from src.core.topic_pool import list_topics
        return list_topics(_g()["cfg"], _g()["conn"], q=q, dimension=dimension, source=source,
                           status=status, limit=limit, offset=offset)

    @app.post("/api/topics")
    def create_topic(req: CreateTopicRequest):
        from src.core.topic_pool import add_manual_topic
        try:
            return add_manual_topic(_g()["conn"], title=req.title, dimension=req.dimension,
                                    summary=req.summary, link=req.link)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/topics/delete")
    def delete_topics(req: IdsRequest):
        """从选题池删除（软删除）：之后不再参与评分与生产，可在「已删除」里恢复。"""
        from src.core.topic_pool import exclude_topics
        return exclude_topics(_g()["cfg"], _g()["conn"], req.ids, req.reason)

    @app.delete("/api/topics/{topic_id}")
    def delete_topic(topic_id: str):
        from src.core.topic_pool import exclude_topics
        res = exclude_topics(_g()["cfg"], _g()["conn"], [topic_id])
        if not res["excluded"]:
            raise HTTPException(404, f"选题 {topic_id} 不在选题池")
        return res

    @app.post("/api/topics/restore")
    def restore_topic_ids(req: IdsRequest):
        from src.core.topic_pool import restore_topics
        return restore_topics(_g()["conn"], req.ids)

    @app.post("/api/topics/purge")
    def purge_topic_ids(req: IdsRequest):
        """彻底删除手动 / 热点选题（KB 文件里的选题只能软删除；已产出内容的选题保留）。"""
        from src.core.topic_pool import purge_topics
        return purge_topics(_g()["conn"], req.ids)

    # --- Agent 编排（§21）---
    @app.get("/api/orchestration")
    def get_orchestration():
        from src.core import orchestration as orch
        return orch.view(_g()["conn"])

    @app.put("/api/orchestration")
    def put_orchestration(req: OrchestrationRequest):
        from src.core import orchestration as orch
        try:
            orch.save_steps(_g()["conn"], req.steps)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return orch.view(_g()["conn"])

    @app.post("/api/orchestration/validate")
    def validate_orchestration(req: OrchestrationRequest):
        from src.core import orchestration as orch
        errors = orch.validate_steps([{"id": s.get("id"), "enabled": bool(s.get("enabled"))} for s in req.steps])
        return {"valid": not errors, "errors": errors}

    @app.post("/api/orchestration/reset")
    def reset_orchestration():
        from src.core import orchestration as orch
        orch.save_steps(_g()["conn"], orch.default_steps(), author="web-ui:reset")
        return orch.view(_g()["conn"])

    @app.get("/api/orchestration/runs")
    def orchestration_runs(limit: int = Query(10, ge=1, le=50)):
        """最近几轮各编排步骤的执行状态 / 耗时（编排预览「最近执行」）。"""
        from src.core.orchestration import Orchestration, step_status_from_report
        conn = _g()["conn"]
        orch_now = Orchestration.load(conn)
        out = []
        for r in conn.execute(
            "SELECT id, round_number, status, trigger_source, created_at, run_report "
            "FROM spiral_rounds ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall():
            rep_ = None
            if r["run_report"]:
                try:
                    rep_ = json.loads(r["run_report"])
                except (TypeError, ValueError):
                    rep_ = None
            duration = None
            if rep_ and rep_.get("started_at") and rep_.get("finished_at"):
                try:
                    duration = round((datetime.fromisoformat(rep_["finished_at"])
                                      - datetime.fromisoformat(rep_["started_at"])).total_seconds(), 1)
                except ValueError:
                    duration = None
            out.append({
                "round_id": r["id"], "round_number": r["round_number"], "status": r["status"],
                "trigger_source": r["trigger_source"], "created_at": r["created_at"],
                "duration_seconds": duration,
                "step_status": (rep_ or {}).get("step_status") or step_status_from_report(rep_, orch_now),
                "errors": (rep_ or {}).get("errors", [])[:5],
                "has_report": rep_ is not None,
            })
        return {"runs": out}

    @app.post("/api/diagnostics")
    def run_diagnostics(req: DiagnoseRequest):
        """8 点端到端流程诊断（§21.3）。"""
        from src.core.orchestration import diagnose
        return diagnose(_g()["cfg"], _g()["conn"], cb=_g()["cb"], live_llm=req.live_llm)

    # --- 删除：已生产内容 / 轮次 ---
    @app.post("/api/contents/delete")
    def delete_content_ids(req: IdsRequest):
        from src.core.content_admin import delete_contents
        return delete_contents(_g()["cfg"], _g()["conn"], req.ids)

    @app.delete("/api/contents/{content_id}")
    def delete_content(content_id: str):
        conn = _g()["conn"]
        row = _content_row(conn, content_id)
        _ensure_content_round_idle(row, "删除内容")
        from src.core.content_admin import delete_contents
        res = delete_contents(_g()["cfg"], conn, [content_id])
        if res["not_found"]:
            raise HTTPException(404, f"内容 {content_id} 不存在")
        if res["blocked"]:
            raise HTTPException(409, res["blocked"][0]["reason"])
        return res

    @app.delete("/api/rounds/{round_id}")
    def delete_round_api(round_id: int):
        from src.core.content_admin import DeleteBlocked, delete_round
        if round_id in _running_rounds:
            raise HTTPException(409, f"轮次 {round_id} 正在执行，不能删除")
        try:
            return delete_round(_g()["cfg"], _g()["conn"], round_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except DeleteBlocked as exc:
            raise HTTPException(409, str(exc)) from exc

    # --- G-09 内容列表 / 编辑 / 重检 / 驳回 ---
    def _content_row(conn, content_id: str):
        row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"内容 {content_id} 不存在")
        return row

    def _ensure_content_round_idle(row, operation: str) -> None:
        round_id = row["round_id"]
        if round_id is not None and round_id in _running_rounds:
            raise HTTPException(
                409,
                f"轮次 {round_id} 正在执行生产/存草稿操作，暂不能{operation}；请等待本轮结束",
            )

    @app.get("/api/contents")
    def list_contents(
        round_id: int = Query(..., description="轮次 ID"),
        platform: Optional[str] = Query(None, pattern="^(xhs|douyin|kuaishou|wechat_channels)$"),
    ):
        conn = _g()["conn"]
        sql = (
            "SELECT c.*, d.id AS draft_id, d.status AS draft_status, "
            "       d.error_msg AS draft_error, d.saved_at AS draft_saved_at, "
            "       d.user_confirmed_at AS draft_user_confirmed_at "
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
            # G-36：只回传文件名，下载走 /api/contents/{id}/images/{i}（不暴露绝对路径）
            row["image_paths"] = [Path(str(p)).name for p in paths]
            items.append(row)
        return {"round_id": round_id, "items": items}

    @app.get("/api/contents/{content_id}/images/{index}")
    def get_content_image(content_id: str, index: int, download: bool = Query(False)):
        """G-36：读取已生成的卡片图（只允许该内容登记过、且在本项目 output 目录下的文件）。

        默认 inline 返回，浏览器可直接预览；download=1 时才带 attachment 触发下载。
        """
        conn = _g()["conn"]
        cfg = _g()["cfg"]
        row = _content_row(conn, content_id)
        try:
            paths = json.loads(row["image_paths"] or "[]")
        except (TypeError, ValueError):
            paths = []
        if not isinstance(paths, list) or index < 0 or index >= len(paths):
            raise HTTPException(404, "图片序号超出范围")
        root = cfg.resolve_path(cfg.paths.output_dir).resolve()
        path = Path(str(paths[index])).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(404, "图片文件不存在（可能已被清理）")
        if download:
            return FileResponse(str(path), filename=path.name, media_type="image/png")
        return FileResponse(str(path), media_type="image/png")

    @app.patch("/api/contents/{content_id}")
    def update_content(content_id: str, req: UpdateContentRequest):
        """人工编辑未发布文案 → 写 user_override 留痕；已发布内容保持不可变。"""
        conn = _g()["conn"]
        row = _content_row(conn, content_id)
        _ensure_content_round_idle(row, "编辑内容")
        published = conn.execute(
            "SELECT status FROM drafts WHERE content_id = ? "
            "AND (user_confirmed_at IS NOT NULL "
            "     OR status IN ('user_published', 'data_fetching', 'published')) "
            "ORDER BY rowid DESC LIMIT 1",
            (content_id,),
        ).fetchone()
        if published:
            raise HTTPException(
                409,
                "该内容已确认发布或已进入数据采集，不能原地修改历史记录；请新建一篇修订内容",
            )

        title = req.title if req.title is not None else row["title"]
        body = req.body if req.body is not None else row["body"]
        tags_json = (
            json.dumps(req.tags, ensure_ascii=False)
            if req.tags is not None else row["tags"]
        )
        changed = (
            title != row["title"] or body != row["body"] or tags_json != row["tags"]
        )

        if changed:
            # G2 结果绑定的是具体文案版本。修改标题/正文/标签后，旧的 PASS 不能继续显示为有效。
            conn.execute(
                "UPDATE contents SET title = ?, body = ?, tags = ?, word_count = ?, "
                "guard_outcome = NULL, guard_reason = NULL WHERE id = ?",
                (title, body, tags_json, len(body or ""), content_id),
            )
        else:
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

    @app.post("/api/contents/{content_id}/cards")
    def regenerate_cards(content_id: str):
        """按当前标题/正文重新出图（封面 + 正文分页），补上历史内容的「只有文没有图」。"""
        conn = _g()["conn"]
        cfg = _g()["cfg"]
        row = _content_row(conn, content_id)
        try:
            title, body = row["title"] or "", row["body"] or ""
            platform = Platform(row["platform"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, f"内容平台非法：{content_id}") from exc
        dimension = ""
        try:
            topic = conn.execute(
                "SELECT dimension FROM topics WHERE id = ?", (row["topic_id"],)
            ).fetchone()
            dimension = (topic["dimension"] if topic else "") or ""
        except Exception:  # noqa: BLE001 — 维度只是装饰，取不到不影响出图
            dimension = ""

        from ..core.content_pipeline import make_card_set, card_footer
        output_dir = cfg.resolve_path(cfg.paths.output_dir)
        paths = make_card_set(title, dimension or "知识卡片", body, output_dir,
                              platform, card_footer(cfg, platform))
        old = []
        try:
            old = json.loads(row["image_paths"] or "[]")
        except (TypeError, ValueError):
            old = []
        conn.execute(
            "UPDATE contents SET image_paths = ? WHERE id = ?",
            (json.dumps([str(p) for p in paths], ensure_ascii=False), content_id),
        )
        conn.commit()
        # 旧图（旧版单封面）顺手清掉，避免 output 目录里堆孤儿文件
        for p in old:
            try:
                Path(str(p)).unlink()
            except OSError:
                pass
        return {"id": content_id, "image_count": len(paths),
                "image_paths": [p.name for p in paths]}

    @app.post("/api/contents/{content_id}/recheck")
    def recheck_content(content_id: str):
        """对编辑后的文案重新跑 G2（R2：检查器异常/无法解析 → 不能默认通过）。"""
        conn = _g()["conn"]
        guard = _g()["guard"]
        row = _content_row(conn, content_id)
        _ensure_content_round_idle(row, "重新检查内容")
        try:
            tags = json.loads(row["tags"]) if row["tags"] else []
        except (TypeError, ValueError):
            tags = []

        try:
            plat = row["platform"] if "platform" in row.keys() else None
            outcome = guard.check_content(row["title"], row["body"], tags, platform=plat)
        except Exception as exc:  # noqa: BLE001 — R2 红线
            logger.exception("G2 重检异常 content=%s", content_id)
            raise HTTPException(500, f"检查器异常，不能默认通过：{redact(str(exc))}") from exc

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
        """人工驳回与 G2 自动质检分开留痕；驳回后禁止正常存草稿/确认发布。"""
        conn = _g()["conn"]
        row = _content_row(conn, content_id)
        _ensure_content_round_idle(row, "驳回内容")
        published = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE content_id = ? "
            "AND (user_confirmed_at IS NOT NULL "
            "OR status IN ('user_published','data_fetching','published'))",
            (content_id,),
        ).fetchone()[0]
        if published:
            raise HTTPException(409, "该内容已有发布历史，不能再按待发布内容驳回；请在复盘中处理")
        eligible = conn.execute(
            "SELECT id, status FROM drafts WHERE content_id = ? "
            "AND status IN ('draft_pending','draft_saved')",
            (content_id,),
        ).fetchall()
        if not eligible:
            raise HTTPException(409, "当前内容没有可驳回的待存内容或平台草稿")
        reason = req.reason.strip()
        message = f"[已驳回] {reason}"
        conn.execute(
            "UPDATE contents SET review_rejected = 1, review_rejection_reason = ? WHERE id = ?",
            (reason, content_id),
        )
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
                "rejected", reason, "web-ui",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        platform_drafts_left = sum(1 for draft in eligible if draft["status"] == "draft_saved")
        return {
            "id": content_id,
            "affected_drafts": cur.rowcount,
            "review_rejected": True,
            "platform_drafts_left": platform_drafts_left,
            "message": message,
        }

    @app.post("/api/contents/{content_id}/resubmit")
    def resubmit_rejected_content(content_id: str, req: ResubmitContentRequest):
        """人工驳回后的内容必须先重新过当前 G2，再显式提交审核解除拦截。"""
        conn = _g()["conn"]
        row = _content_row(conn, content_id)
        _ensure_content_round_idle(row, "重新提交内容")
        if not bool(row["review_rejected"]):
            raise HTTPException(409, "该内容当前没有人工驳回状态")
        if conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE content_id = ? "
            "AND (user_confirmed_at IS NOT NULL "
            "OR status IN ('user_published','data_fetching','published'))",
            (content_id,),
        ).fetchone()[0]:
            raise HTTPException(409, "该内容已有发布历史，不能重新提交待发布审核")
        from src.core.content_pipeline import _llm_available
        outcome = row["guard_outcome"]
        passes = outcome == "pass" or (
            outcome == "warn" and not _llm_available(_g()["cfg"])
        )
        if not passes:
            raise HTTPException(
                409,
                f"当前质检状态为 {outcome or '未检查'}，须先修改文案并重新检查通过后才能重新提交审核",
            )
        reason = req.reason.strip()
        conn.execute(
            "UPDATE contents SET review_rejected = 0, review_rejection_reason = NULL WHERE id = ?",
            (content_id,),
        )
        conn.execute(
            "UPDATE drafts SET error_msg = NULL WHERE content_id = ? "
            "AND status IN ('draft_pending','draft_saved') "
            "AND error_msg LIKE '[已驳回]%'",
            (content_id,),
        )
        conn.execute(
            "INSERT INTO user_override (round_id, override_field, original_value, "
            "override_value, reason, overridden_by, overridden_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["round_id"], "review_resubmit",
                json.dumps({"review_rejection_reason": row["review_rejection_reason"]}, ensure_ascii=False)[:500],
                "resubmitted", reason, "web-ui",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        return {"id": content_id, "review_rejected": False, "message": "已重新提交审核"}

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
        """选题池评分预览（与生产同一来源：本地选题库 + 热点转入 + 手动录入，已删除的不参与）。"""
        from src.core.topic_pool import load_pool
        topics, _pool_report = load_pool(_g()["cfg"], _g()["conn"])
        if not topics:
            return {"mode": _g()["scoring"]._determine_mode(), "topics": [], "message": "选题库暂无数据"}
        feedback = None
        if _g()["cfg"].autogrow.apply_feedback:
            from ..core.autogrow import build_feedback
            feedback = build_feedback(_g()["cfg"], _g()["conn"], _g()["guard"])
        scored = _g()["scoring"].score_topics(topics, feedback=feedback)
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
                for s in scored[:limit]
            ],
        }

    # --- 热点雷达（R22：采集失败不显示数字）---
    @app.get("/api/hot_topics")
    def hot_topics(refresh: bool = Query(False)):
        """热点雷达：只读库（采集 + 标签匹配由后台定时任务完成）。

        refresh=true 兼容旧前端：触发一次后台刷新后立即返回当前库内数据，不阻塞请求。
        """
        from ..core import hot_topic_service

        conn = _g()["conn"]
        cfg = _g()["cfg"]
        if refresh:
            hot_topic_service.trigger_background(cfg, trigger="manual")
        data = HotTopicCollector(cfg, conn).overview()
        data["refresh_job"] = hot_topic_service.status()
        data["research_job"] = hot_topic_service.research_status()
        # 研判依赖大模型：未启用时前端把「研判热点」按钮置灰并提示去系统设置开启
        from ..core.hot_topic_matcher import default_llm_fn
        data["research_ready"] = default_llm_fn(cfg) is not None
        # 多平台画像：标出每条热点适合哪些平台的账号（不同平台可能是不同定位）
        from ..core.persona import matching_platforms
        if cfg.ultimate_goal.goal_mode != "unified":
            for item in data.get("items") or []:
                item["fit_platforms"] = matching_platforms(
                    cfg, f"{item.get('title') or ''} {item.get('match_angle') or ''} {item.get('dimension') or ''}"
                )
        return data

    @app.post("/api/hot_topics/refresh", status_code=202)
    def hot_topics_refresh():
        """手动触发后台采集 + 标签匹配，立即返回任务状态（前端轮询 GET /api/hot_topics 的 refresh_job）。"""
        from ..core import hot_topic_service

        cfg = _g()["cfg"]
        if cfg.hot_topics.enabled is False:
            raise HTTPException(409, "热点采集已在配置中关闭（hot_topics.enabled=false）")
        return hot_topic_service.trigger_background(cfg, trigger="manual")

    @app.post("/api/hot_topics/research", status_code=202)
    def hot_topics_research():
        """手动触发大模型研判（只研判当前榜单，历史热点不参与），立即返回任务状态。

        前端轮询 GET /api/hot_topics 的 research_job 判断是否完成。
        """
        from ..core import hot_topic_service
        from ..core.hot_topic_matcher import default_llm_fn

        cfg = _g()["cfg"]
        if default_llm_fn(cfg) is None:
            raise HTTPException(409, "大模型未启用：请到「系统设置」开启主模型并配置 API Key")
        return hot_topic_service.trigger_research(cfg)

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
            "SELECT id, excluded_at FROM topic_index WHERE id = ?", (topic_id,)
        ).fetchone()
        if existing:
            if existing["excluded_at"]:
                conn.execute("UPDATE topic_index SET excluded_at = NULL, excluded_reason = NULL WHERE id = ?",
                             (topic_id,))
                conn.commit()
                return {"id": topic_id, "imported": True, "message": "该热点之前被删除，已恢复到选题池"}
            return {"id": topic_id, "imported": False, "message": "选题库已存在该热点"}

        conn.execute(
            "INSERT INTO topic_index (id, title, dimension, source, date, first_seen_at, link, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                topic_id, title, dimension or None,
                f"hot:{source}", datetime.now().strftime("%Y-%m-%d"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                raw_url, str((req or {}).get("summary") or "").strip() or None,
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
        return {"results": notifier.send_test(), "macos_available": macos_available()}

    @app.post("/api/notify/webhook")
    def save_notify_webhook(req: dict):
        """企微 / 飞书 Webhook URL 写入 .env（R23：永不回传明文），可顺手启用该通道。"""
        channel = str((req or {}).get("channel") or "").strip()
        if channel not in ("wecom", "feishu"):
            raise HTTPException(400, "channel 只能是 wecom / feishu")
        url = str((req or {}).get("url") or "").strip()
        if url and not url.startswith(("https://", "http://")):
            raise HTTPException(422, "Webhook 地址必须是 http(s) URL")
        if url:
            try:
                kb_sources._validate_remote_url(url)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        cfg = _g()["cfg"]
        ccfg = cfg.notifications.channels.get(channel)
        env_name = (getattr(ccfg, "webhook_url_env", None) or
                    ("WECOM_WEBHOOK_URL" if channel == "wecom" else "FEISHU_WEBHOOK_URL"))
        if not url and not read_env(env_name):
            raise HTTPException(422, "Webhook 地址不能为空")
        write_env(env_name, url)
        if url and bool((req or {}).get("enabled", True)):
            try:
                patch_config(cfg, _g()["conn"], {f"notifications.channels.{channel}.enabled": True})
            except (ConfigNotAllowedError, ConfigPatchError) as e:
                raise HTTPException(422, str(e)) from e
        return {"ok": True, "channel": channel, "enabled": bool(getattr(ccfg, "enabled", False)),
                "has_webhook_url": bool(read_env(env_name)), "env_name": env_name}

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
        # R17 收口：同步回写运营日志 ops_logs（引擎对 active_comment/follow 写的 pending_verify）。
        # ops_logs 无 task_run 外键，且引擎写 ops_logs 时未带 round_id（该列恒为 NULL），
        # 故只能按"同平台"近似收口该平台的待核实操作（pending_verify 量级小，且本就需用户核实）。
        row2 = conn.execute(
            "SELECT platform FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row2 and row2["platform"]:
            conn.execute(
                "UPDATE ops_logs SET status = ? WHERE platform = ? AND status = 'pending_verify'",
                (target, row2["platform"]),
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
    # --- 自动闭环（自生长）---
    @app.get("/api/autogrow")
    def autogrow_status():
        """自动闭环状态：开关与参数、现在能否开下一轮（及原因）、将吃进的复盘结论、最近执行记录。"""
        from ..core.autogrow import build_feedback
        cfg = _g()["cfg"]
        ctl = _g()["autogrow"]
        feedback = build_feedback(cfg, _g()["conn"], _g()["guard"])
        job = _g()["scheduler"].scheduler.get_job("autogrow_next_round")
        return {
            "config": cfg.autogrow.model_dump(),
            "job_enabled": cfg.scheduler.job("autogrow_next_round").enabled,
            "next_check_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
            "verdict": ctl.evaluate(),
            "feedback": feedback.to_dict(),
            "recent_runs": ctl.recent_runs(),
        }

    @app.post("/api/autogrow/run")
    async def autogrow_run_now():
        """立即检查并（条件满足时）开下一轮。只跳过冷却期与每日上限；
        活跃轮次 / 目标达成 / 连续失败 / 全平台熔断仍会拦住。"""
        result = await _g()["autogrow"].tick(force=True)
        result.pop("report", None)
        return result

    @app.get("/api/scheduler/jobs")
    def scheduler_jobs():
        """定时任务：配置（开关 / 间隔 / cron）+ APScheduler 实际排期。关闭的任务也会列出。"""
        sched = _g()["scheduler"]
        cfg = _g()["cfg"]
        registered = {job.id: job for job in sched.scheduler.get_jobs()}
        result = []
        for job_id, conf in scheduler_jobs_view(cfg).items():
            job = registered.pop(job_id, None)
            result.append({
                "id": job_id,
                "name": conf["description"],
                "enabled": conf["enabled"],
                "interval_minutes": conf["interval_minutes"],
                "cron": conf["cron"],
                "schedule": conf["schedule"],
                "next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None,
                "trigger": str(job.trigger) if job else None,
                "configurable": True,
            })
        # 动态注册的一次性任务（data_fetch_* 等）只展示，不可配置
        for job in registered.values():
            result.append({
                "id": job.id, "name": job.name or job.id, "enabled": True,
                "interval_minutes": None, "cron": None, "schedule": str(job.trigger),
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger), "configurable": False,
            })
        return {"jobs": result, "count": len(result), "running": sched.scheduler.running}

    @app.post("/api/scheduler/jobs/{job_id}/run")
    def scheduler_run_job(job_id: str):
        """立即触发一次定时任务（§8.3 验证要求：手动触发并确认本地时区）。"""
        sched = _g()["scheduler"]
        job = sched.scheduler.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"任务不存在或已关闭: {job_id}")
        job.modify(next_run_time=datetime.now())
        return {"job_id": job_id, "triggered_at": datetime.now().isoformat(timespec="seconds")}

    # --- 节奏调度配置（只读）---
    @app.get("/api/rhythm")
    def rhythm_config():
        """平台独立配额 / 时段窗口（RhythmController 生效中的配置）。

        返回结构与前端 RhythmConfig 接口严格对齐：
        {
          enabled_platforms: [...],
          quotas: { platform: { passive_replies, active_comments, follows } },
          posting_windows: { platform: ["08:00-23:00"] },
          min_interval_seconds: int,
        }
        """
        cfg = _g()["cfg"]
        enabled = cfg.platforms.enabled_keys()
        quotas_map = getattr(cfg.rhythm, "daily_quotas", None) or {}
        windows = cfg.rhythm.windows or {}

        # ops 时段窗口（所有平台共用）
        ops_win = windows.get("ops")
        ops_range = f"{ops_win.start_hour:02d}:00-{ops_win.end_hour:02d}:00" if ops_win else "08:00-23:00"

        # 每平台 posting_windows：目前四平台共用 ops 窗口（后续可分平台配）
        posting_windows = {p: [ops_range] for p in enabled}

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
            "posting_windows": posting_windows,
            "ops_window": (
                {"start_hour": ops_win.start_hour, "end_hour": ops_win.end_hour}
                if ops_win else None
            ),
            "intervals": cfg.rhythm.intervals.model_dump(),
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
        # 定时任务配置改动 → 立即重排（不用重启）
        sched = g2.get("scheduler")
        rescheduled = []
        if sched is not None:
            for job_id in sorted(changed_job_ids(c["key"] for c in result["changed"])):
                try:
                    rescheduled.append(sched.apply_job_config(job_id))
                except Exception as e:  # noqa: BLE001
                    logger.exception("重排定时任务失败 %s", job_id)
                    rescheduled.append({"job_id": job_id, "error": redact(str(e))})
        result["rescheduled"] = rescheduled
        return {**result, "config": current_config_view(g2["cfg"], g2["conn"])["editable"]}

    @app.post("/api/config/llm_key")
    def save_llm_api_key(req: LlmKeyRequest):
        """把 API Key 写入 config/.env（chmod 600），立即生效；永不回传明文。"""
        cfg = _g()["cfg"]
        key_name = cfg.llm.api_key_env
        value = req.api_key.strip()
        write_env(key_name, value)
        # R23：只回报状态，绝不回报 Key 本身
        return {"ok": True, "has_key": bool(os.environ.get(key_name)),
                "env_file": str(env_file())}

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

    # --- 选题库来源（上传 / 远程 / 服务器路径）---
    @app.get("/api/kb/sources")
    def kb_sources_list():
        return kb_sources.list_sources(_g()["cfg"])

    @app.post("/api/kb/sources/upload")
    def kb_source_upload(req: dict):
        cfg = _g()["cfg"]
        try:
            source = kb_sources.add_upload(
                cfg,
                filename=str(req.get("filename") or ""),
                content_b64=str(req.get("content") or ""),
                name=str(req.get("name") or ""),
                field_mapping=req.get("field_mapping") or None,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except (FileNotFoundError, OSError) as e:
            raise HTTPException(400, str(e)) from e
        return source

    @app.post("/api/kb/sources/server")
    def kb_source_server(req: dict):
        cfg = _g()["cfg"]
        try:
            return kb_sources.add_server(
                cfg, path=str(req.get("path") or ""), name=str(req.get("name") or ""),
                fmt=str(req.get("format") or "auto"),
                field_mapping=req.get("field_mapping") or None,
            )
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/kb/sources/remote")
    def kb_source_remote(req: dict):
        cfg = _g()["cfg"]
        try:
            return kb_sources.add_remote(
                cfg, url=str(req.get("url") or ""), name=str(req.get("name") or ""),
                fmt=str(req.get("format") or "auto"),
                headers=req.get("headers") or None,
                field_mapping=req.get("field_mapping") or None,
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"远程地址请求失败：{redact(str(e))}") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/kb/sources/default")
    def kb_source_default():
        return kb_sources.add_default_empty(_g()["cfg"])

    @app.patch("/api/kb/sources/{source_id}")
    def kb_source_update(source_id: str, req: dict):
        try:
            return kb_sources.update_source(_g()["cfg"], source_id, req or {})
        except KeyError:
            raise HTTPException(404, "来源不存在")
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.delete("/api/kb/sources/{source_id}")
    def kb_source_delete(source_id: str):
        try:
            return kb_sources.delete_source(_g()["cfg"], source_id)
        except KeyError:
            raise HTTPException(404, "来源不存在")

    @app.post("/api/kb/import")
    async def kb_import_to_pool():
        """立即把选题库来源并入选题池（与每天 6:00 的 kb_scan 同一逻辑，手动触发一次）。"""
        sched = _g()["scheduler"]
        conn = _g()["conn"]
        before = conn.execute("SELECT COUNT(*) FROM topic_index").fetchone()[0]
        await sched._job_kb_scan()
        total = conn.execute("SELECT COUNT(*) FROM topic_index").fetchone()[0]
        return {"total": total, "inserted": total - before}

    @app.post("/api/kb/sources/{source_id}/sync")
    def kb_source_sync(source_id: str):
        cfg = _g()["cfg"]
        source = next((s for s in kb_sources.load_registry(cfg) if s["id"] == source_id), None)
        if source is None:
            raise HTTPException(404, "来源不存在")
        if source.get("kind") != "remote":
            raise HTTPException(400, "只有远程来源需要同步")
        try:
            info = kb_sources.sync_remote(cfg, source)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"远程地址请求失败：{redact(str(e))}") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        sources = kb_sources.load_registry(cfg)
        for item in sources:
            if item["id"] == source_id:
                item.update({k: v for k, v in info.items() if k != "cache_path"})
        kb_sources.save_registry(cfg, sources)
        return {"id": source_id, "synced_at": info["last_sync_at"], "rows": info["rows"]}

    @app.get("/api/kb/sources/{source_id}/preview")
    def kb_source_preview(source_id: str, limit: int = Query(5, ge=1, le=20)):
        try:
            return kb_sources.preview_source(_g()["cfg"], source_id, limit=limit)
        except KeyError:
            raise HTTPException(404, "来源不存在")
        except Exception as e:  # noqa: BLE001 — 预览失败返回可读原因
            raise HTTPException(422, redact(str(e))) from e

    # --- 选题库 schema 检查（兼容旧入口：读第一个服务器路径来源）---
    @app.get("/api/kb/inspect")
    def kb_inspect():
        return kb_sources.legacy_inspect(_g()["cfg"])

    # --- 行业模板（一键填画像 / 维度建议 / 合规红线）---
    @app.get("/api/presets")
    def list_presets():
        return {"items": industry_presets.list_presets()}

    @app.post("/api/presets/{key}/apply")
    def apply_preset(key: str):
        preset = industry_presets.get_preset(key)
        if preset is None:
            raise HTTPException(404, "行业模板不存在")
        cfg = _g()["cfg"]
        updates = {
            "ultimate_goal.positioning": preset["positioning"],
            "ultimate_goal.persona_keywords": list(preset["persona_keywords"]),
            "ultimate_goal.forbidden_directions": list(preset["forbidden_directions"]),
            "ultimate_goal.redline_keywords": sorted(
                set(cfg.ultimate_goal.redline_keywords) | set(preset["extra_redlines"])
            ),
        }
        try:
            patch_config(cfg, _g()["conn"], updates)
        except (ConfigNotAllowedError, ConfigPatchError) as e:
            raise HTTPException(422, str(e)) from e
        return {
            "key": key, "name": preset["name"],
            "positioning": preset["positioning"],
            "persona_keywords": preset["persona_keywords"],
            "dimensions": preset["dimensions"],
            "platform_notes": preset["platform_notes"],
            "extra_redlines": preset["extra_redlines"],
            "message": "画像与红线已更新；建议维度可在「内容生产」里填入母计划",
        }

    # --- 热点源（综合热榜可关，可加 RSS / JSON 行业源）---
    def _public_hot_sources(items: list[dict]) -> list[dict]:
        # URL 可能带签名/访问令牌；前端只需显示主机与脱敏占位符。
        public = []
        for item in items or []:
            row = dict(item)
            raw_url = str(row.get("url") or "")
            row["url"] = kb_sources._redact_remote_url(raw_url) if raw_url else ""
            raw_name = str(row.get("name") or "")
            row["name"] = redact(raw_name) if raw_name else raw_name
            public.append(row)
        return public

    def _restore_masked_hot_source_urls(items: list[dict], current: list[dict]) -> list[dict]:
        # 列表操作会把脱敏后 URL 一起提交；按来源标识恢复服务器保存的原始 URL。
        # 新增项的 URL 不匹配已有项的脱敏值，因此仍会正常保存。
        restored = []
        for incoming in items or []:
            row = dict(incoming)
            for old in current or []:
                old_name = str(old.get("name") or "")
                incoming_name = str(row.get("name") or "")
                same_name = old_name == incoming_name or redact(old_name) == incoming_name
                same_key = (
                    same_name
                    and str(old.get("type") or "aggregate") == str(row.get("type") or "aggregate")
                    and str(old.get("platform") or "") == str(row.get("platform") or "")
                    and tuple(old.get("platforms") or []) == tuple(row.get("platforms") or [])
                )
                if same_key and str(row.get("url") or "") == kb_sources._redact_remote_url(str(old.get("url") or "")):
                    row["url"] = old.get("url") or ""
                    break
            restored.append(row)
        return restored

    @app.get("/api/hot_topics/sources")
    def hot_sources_list():
        return {"items": _public_hot_sources(_g()["cfg"].hot_topics.sources)}

    @app.put("/api/hot_topics/sources")
    def hot_sources_update(req: dict):
        cfg = _g()["cfg"]
        items = (req or {}).get("items")
        if not isinstance(items, list):
            raise HTTPException(422, "热点源必须是数组")
        items = _restore_masked_hot_source_urls(items, cfg.hot_topics.sources)
        try:
            patch_config(cfg, _g()["conn"], {"hot_topics.sources": items})
        except (ConfigNotAllowedError, ConfigPatchError) as e:
            raise HTTPException(422, str(e)) from e
        return {"items": _public_hot_sources(cfg.hot_topics.sources)}

    # ============================================================
    # Dashboard 补齐（对比报告 3.1 🔴 后端真缺失）
    # ============================================================

    # --- 四平台进度聚合（Dashboard #11：四个平台独立进度）---
    @app.get("/api/platform_progress")
    def platform_progress():
        """返回全部 4 个平台的：是否启用 / 总草稿数 / 各状态分布 / 熔断状态。

        启用判定与 /api/status 共用 cfg.platforms.enabled_keys()，避免两处口径不一致；
        未启用的平台也返回（enabled=false），前端四平台 lane 不必自行补齐。
        """
        conn = _g()["conn"]
        cfg = _g()["cfg"]
        enabled_keys = set(cfg.platforms.enabled_keys())
        result = {}
        for p in [pl.value for pl in Platform]:
            # 各状态计数
            status_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM drafts WHERE platform=? GROUP BY status",
                (p,),
            ).fetchall()
            status_map = {r[0]: r[1] for r in status_rows}
            # 总数
            total = sum(status_map.values())
            # 熔断状态（resolved_at IS NULL 即熔断中）
            cb_row = conn.execute(
                "SELECT COUNT(*) FROM circuit_breaker_events "
                "WHERE platform=? AND resolved_at IS NULL",
                (p,),
            ).fetchone()
            cb = cb_row[0] > 0
            result[p] = {
                "enabled": p in enabled_keys,
                "total_drafts": total,
                "status_breakdown": status_map,
                "circuit_broken": cb,
            }
        return result

    # --- 决策待办聚合（Dashboard #9：需要你决策的事项）---
    @app.get("/api/action_items")
    def action_items():
        """聚合三类需要人工决策的事项：

        1. goals：有 target_platform、但 achievement_rate 为 NULL 且 baseline_at 过期
        2. rounds：status='awaiting_publish' 超过 72h（R8 publish_confirm_timeout）
        3. circuit_breaker：cooldown_until 已到期、但 resolved_at 仍 NULL（需要 try_recover）

        返回统一 action_items[] 列表，前端按 type 分组显示。
        """
        from datetime import datetime, timedelta as td
        conn = _g()["conn"]
        now = datetime.now()
        items: list[dict] = []

        def _pname(p: Optional[str]) -> str:
            if not p:
                return "全平台"
            try:
                return Platform(p).display_name
            except ValueError:
                return p

        # 1. 目标已过统计周期仍未达成：达成率与 /api/goals 同一口径（GoalProgressUpdater.rate_for），
        #    metric_period_end 是日期，必须按「日期 < 今天」比较，否则截止当天就会被误判超期
        goal_rows = conn.execute(
            """SELECT id, goal_text, target_platform, target_metric, target_value,
                      metric_scope, baseline_value, baseline_at, current_value, metric_period_end
               FROM goals
               WHERE status='active'
                 AND metric_period_end IS NOT NULL
                 AND substr(metric_period_end, 1, 10) < ?
            """,
            (now.date().isoformat(),),
        ).fetchall()
        for r in goal_rows:
            rate = GoalProgressUpdater.rate_for(r)
            if rate is not None and rate >= 100:
                continue
            pct = f"{rate:.0f}%" if rate is not None else "数据待采集"
            cur_val = r["current_value"]
            items.append({
                "id": f"goal:{r['id']}",
                "type": "goal_strategy",
                "title": f"目标「{r['goal_text']}」已过截止日期 {r['metric_period_end'][:10]} 但未达成（{pct}）",
                "detail": (
                    f"{_pname(r['target_platform'])} · {r['target_metric']} 目标 {r['target_value']}"
                    f" · 当前 {cur_val if cur_val is not None else '待采集'}"
                ),
                "priority": "high",
                "source": "goals",
                "platform": r["target_platform"],
                "created_at": r["metric_period_end"],
                "actions": ["go_goal"],
            })

        # 2. 轮次等待发布超过 72h
        rounds_rows = conn.execute(
            """SELECT id, round_number, status, awaiting_publish_ended_at, created_at
               FROM spiral_rounds
               WHERE status='awaiting_publish'
                 AND awaiting_publish_ended_at IS NOT NULL
                 AND awaiting_publish_ended_at < ?
            """,
            (now.isoformat(),),
        ).fetchall()
        for r in rounds_rows:
            items.append({
                "id": f"round:{r['id']}",
                "type": "publish_decision",
                "title": f"Round {r['round_number']} 已超过 {r['awaiting_publish_ended_at'][:10]} 等待发布，需确认或取消",
                "detail": f"超时 {(now - datetime.fromisoformat(r['awaiting_publish_ended_at'])).days} 天",
                "priority": "high",
                "source": "rounds",
                "created_at": r["created_at"],
                "actions": ["go_round"],
            })

        # 3. 熔断冷却到期未恢复（需要 try_recover 3 步验证）
        cb_rows = conn.execute(
            """SELECT id, platform, reason, detected_at, cooldown_until, resolved_at
               FROM circuit_breaker_events
               WHERE resolved_at IS NULL
                 AND cooldown_until IS NOT NULL
                 AND cooldown_until < ?
            """,
            (now.isoformat(),),
        ).fetchall()
        for r in cb_rows:
            items.append({
                "id": f"cb:{r['id']}",
                "type": "circuit_recover",
                "title": f"{_pname(r['platform'])} 熔断冷却已到期（原因：{r['reason']}），需执行 3 步恢复验证",
                "platform": r["platform"],
                "detail": f"冷却到期于 {r['cooldown_until'][:16]}",
                "priority": "high",
                "source": "circuit_breaker",
                "created_at": r["detected_at"],
                "actions": ["run_recover"],
            })

        # 4. circuits 熔断但 cooldown 未到期（低优先级提醒）
        cb2 = conn.execute(
            """SELECT id, platform, reason, detected_at, cooldown_until
               FROM circuit_breaker_events
               WHERE resolved_at IS NULL
                 AND (cooldown_until IS NULL OR cooldown_until >= ?)
               LIMIT 5
            """,
            (now.isoformat(),),
        ).fetchall()
        for r in cb2:
            items.append({
                "id": f"cb2:{r['id']}",
                "type": "circuit_active",
                "title": f"{_pname(r['platform'])} 熔断中（原因：{r['reason']}），冷却至 {r['cooldown_until'][:16] if r['cooldown_until'] else '—'}",
                "platform": r["platform"],
                "priority": "medium",
                "source": "circuit_breaker",
                "created_at": r["detected_at"],
                "actions": ["go_ops"],
            })

        return {"items": items, "total": len(items)}

    # ============================================================
    # Operations 补齐（对比报告 3.7 🔴 后端真缺失）
    # ============================================================

    # --- 通知规则配置（目前规则硬编码在 notify 模块，补可编辑 CRUD）---
    @app.get("/api/notify_rules")
    def list_notify_rules():
        conn = _g()["conn"]
        try:
            rows = conn.execute(
                "SELECT * FROM notify_rules ORDER BY id ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            # 表不存在 — 自动建表（首次调用时 lazy create）
            conn.executescript(_NOTIFY_RULES_SCHEMA)
            conn.commit()
            rows = []
        return [dict(r) for r in rows]

    @app.post("/api/notify_rules")
    def create_notify_rule(req: NotifyRuleRequest):
        conn = _g()["conn"]
        try:
            conn.execute("SELECT 1 FROM notify_rules LIMIT 1")
        except sqlite3.OperationalError:
            conn.executescript(_NOTIFY_RULES_SCHEMA); conn.commit()
        now = datetime.now().isoformat(timespec="seconds")  # R5：本地时区
        # 同场景同通道只保留一条（唯一索引兜底；老库没有索引时这里先做 upsert）
        existing = conn.execute(
            "SELECT id FROM notify_rules WHERE scenario=? AND channel=?",
            (req.scenario, req.channel),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE notify_rules SET level=?, enabled=?, condition=?, notes=?,
                          updated_at=? WHERE id=?""",
                (req.level, 1 if req.enabled else 0, req.condition, req.notes,
                 now, existing[0]),
            )
            conn.commit()
            return {"id": existing[0], "message": "同场景同通道规则已存在，已更新"}
        cur = conn.execute(
            """INSERT INTO notify_rules (scenario, channel, level, enabled, condition, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (req.scenario, req.channel, req.level, 1 if req.enabled else 0,
             req.condition, req.notes, now),
        )
        conn.commit()
        return {"id": cur.lastrowid, "message": "创建成功"}

    @app.patch("/api/notify_rules/{rule_id}")
    def update_notify_rule(rule_id: int, req: NotifyRuleRequest):
        conn = _g()["conn"]
        conn.execute(
            """UPDATE notify_rules SET scenario=?, channel=?, level=?, enabled=?,
                      condition=?, notes=?, updated_at=?
               WHERE id=?""",
            (req.scenario, req.channel, req.level, 1 if req.enabled else 0,
             req.condition, req.notes,
             datetime.now().isoformat(timespec="seconds"), rule_id),
        )
        conn.commit()
        return {"id": rule_id, "message": "更新成功"}

    @app.delete("/api/notify_rules/{rule_id}")
    def delete_notify_rule(rule_id: int):
        conn = _g()["conn"]
        conn.execute("DELETE FROM notify_rules WHERE id=?", (rule_id,))
        conn.commit()
        return {"message": "删除成功"}

    # --- 平台文案风格（platform_styles 表，migrations/016）---
    @app.get("/api/platform_styles")
    def list_platform_styles():
        conn = _g()["conn"]
        rows = conn.execute(
            "SELECT platform, title_style, body_style, tone, hashtags, "
            "updated_at, updated_by FROM platform_styles ORDER BY platform"
        ).fetchall()
        items = []
        for r in rows:
            try:
                tags = json.loads(r["hashtags"] or "[]")
            except (TypeError, ValueError):
                tags = []
            items.append({
                "platform": r["platform"],
                "title_style": r["title_style"],
                "body_style": r["body_style"],
                "tone": r["tone"],
                # 存的是不带 # 的标签；空数组 = 只带选题维度标签
                "hashtags": tags if isinstance(tags, list) else [],
                "updated_at": r["updated_at"],
                "updated_by": r["updated_by"],
            })
        return {"items": items}

    @app.patch("/api/platform_styles/{platform}")
    def update_platform_style(platform: str, req: PlatformStyleUpdate):
        """改平台风格 / 话题标签：立即生效，逐字段写 config_changes 审计。"""
        conn = _g()["conn"]
        if platform not in {p.value for p in Platform}:
            raise HTTPException(400, "platform 只能是 xhs / douyin / kuaishou / wechat_channels")
        row = conn.execute(
            "SELECT title_style, body_style, tone, hashtags FROM platform_styles "
            "WHERE platform = ?",
            (platform,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                404, f"{platform} 尚无平台风格记录（请确认迁移 016 已执行）"
            )

        new_values: dict[str, str] = {}
        for field in ("title_style", "body_style", "tone"):
            value = getattr(req, field)
            if value is None:
                continue
            text = value.strip()
            if not text:
                raise HTTPException(422, f"{field} 不能为空")
            new_values[field] = text
        if req.hashtags is not None:
            tags: list[str] = []
            for raw in req.hashtags:
                tag = str(raw).strip().lstrip("#")
                if tag and tag not in tags:
                    tags.append(tag)
            new_values["hashtags"] = json.dumps(tags, ensure_ascii=False)

        old_values = {
            "title_style": row["title_style"],
            "body_style": row["body_style"],
            "tone": row["tone"],
            "hashtags": row["hashtags"] or "[]",
        }
        changed = [
            (k, old_values[k], v) for k, v in new_values.items() if old_values[k] != v
        ]
        if not changed:
            return {"platform": platform, "changed": 0, "message": "没有改动"}

        now = datetime.now().isoformat(timespec="seconds")  # R5：本地时区
        for key, old, new in changed:
            # 审计口径与 config_changes 一致：platform_styles.<platform>.<field>
            conn.execute(
                "INSERT INTO config_changes (key, old_value, new_value, changed_at) "
                "VALUES (?, ?, ?, ?)",
                (f"platform_styles.{platform}.{key}", str(old), str(new), now),
            )
        assignments = ", ".join(f"{k} = ?" for k in new_values)
        conn.execute(
            f"UPDATE platform_styles SET {assignments}, updated_at = ?, updated_by = ? "
            "WHERE platform = ?",
            (*new_values.values(), now, "settings_ui", platform),
        )
        conn.commit()
        return {
            "platform": platform,
            "changed": len(changed),
            "fields": {key: new for key, _old, new in changed},
        }

    # ============================================================
    # P1 — AI 证据审计链（对比报告缺口）
    # ============================================================

    @app.get("/api/ai_calls")
    def list_ai_calls(limit: int = Query(200, ge=1, le=2000), step: str | None = None):
        conn = _g()["conn"]
        sql = "SELECT * FROM ai_calls"
        args: list = []
        if step:
            sql += " WHERE step = ?"
            args.append(step)
        sql += " ORDER BY called_at DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/user_override")
    def list_user_override(limit: int = Query(200, ge=1, le=2000)):
        conn = _g()["conn"]
        rows = conn.execute(
            "SELECT * FROM user_override ORDER BY overridden_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ============================================================
    # 账号管理（扫码登录四平台 — 直接调 Easel publish.py login）
    # ============================================================
    from ..core.account_login import (
        start_login as _start_login,
        get_login_status as _get_login_status,
        get_all_accounts as _get_all_accounts,
        cancel_login as _cancel_login,
        clear_login as _clear_login,
        submit_sms_code as _submit_sms_code,
    )

    class SmsCodeRequest(BaseModel):
        code: str

    def _valid_platform(platform: str) -> str:
        """校验平台代号；非法 raise HTTPException。"""
        from fastapi import HTTPException
        valid = {"xhs", "douyin", "kuaishou", "wechat_channels"}
        if platform not in valid:
            raise HTTPException(400, f"未知平台 {platform}，支持: {sorted(valid)}")
        return platform

    @app.get("/api/accounts")
    def list_accounts():
        """四平台账号状态汇总（Accounts 页顶部四卡片用）。"""
        return {"accounts": _get_all_accounts()}

    @app.post("/api/accounts/{platform}/login")
    def start_platform_login(platform: str, restart: bool = Query(False)):
        """启动后台登录 subprocess（调 Easel vendor xhs_publish.py login / douyin_publish.py login / web_publisher.py login-qr）。

        立即返回当前状态 + QR base64（如果已生成）。前端轮询 GET /api/accounts/{platform}/login_status 拿进度。
        重启登录加 ?restart=true（先 terminate 旧进程再起新的）。
        """
        p = _valid_platform(platform)
        return _start_login(p, restart=restart)

    @app.get("/api/accounts/{platform}/login_status")
    def platform_login_status(platform: str):
        """前端轮询：返回登录进度 + QR base64（data:image/png;base64,... 直嵌 <img>）。"""
        p = _valid_platform(platform)
        return _get_login_status(p)

    @app.post("/api/accounts/{platform}/logout")
    def logout_platform(platform: str):
        """注销：清持久化 profile + storage_state + 登录标记（下次需重新扫码）。"""
        p = _valid_platform(platform)
        return _clear_login(p)

    @app.post("/api/accounts/{platform}/sms")
    def submit_platform_sms(platform: str, req: SmsCodeRequest):
        """回填短信验证码（登录进入 sms_required 时由前端提交，runner 读走后继续登录）。"""
        p = _valid_platform(platform)
        try:
            return _submit_sms_code(p, req.code)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/accounts/{platform}/cancel_login")
    def cancel_platform_login(platform: str):
        """取消进行中的登录（terminate subprocess + 清状态文件）。"""
        p = _valid_platform(platform)
        return _cancel_login(p)

    @app.get("/api/redline_history")
    def list_redline_history(limit: int = Query(200, ge=1, le=2000)):
        """红线拦截历史：从 contents 表中 guard_outcome != 'pass' 的记录聚合"""
        conn = _g()["conn"]
        rows = conn.execute(
            """SELECT id, round_id, platform, title, guard_outcome, guard_reason,
                      created_at
               FROM contents
               WHERE guard_outcome IN ('warn', 'fail', 'error')
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# notify_rules 表 schema（lazy create，首次调用 get/post 时自动建立）
# ============================================================
_NOTIFY_RULES_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario   TEXT NOT NULL,            -- 触发场景（circuit_broken / round_completed / ...）
    channel    TEXT NOT NULL CHECK (channel IN ('macos', 'wecom', 'feishu', 'console')),
    level      TEXT NOT NULL DEFAULT 'warning'
               CHECK (level IN ('info', 'warning', 'error', 'critical')),
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    condition  TEXT,                     -- 条件表达式（SQL fragment / JSON filter）
    notes      TEXT,
    created_at DATETIME,
    updated_at DATETIME
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_notify_rules_scenario_channel
    ON notify_rules (scenario, channel);
"""



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
