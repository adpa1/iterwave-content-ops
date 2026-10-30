"""热点异步采集服务：采集 → 落库 → 标签匹配，单飞（同一时刻只跑一次）。

调用方：
  - APScheduler 定时任务 hot_topics_refresh（频率见 config scheduler.jobs.hot_topics_refresh）
  - POST /api/hot_topics/refresh（手动触发，立即返回，后台线程执行）
  - 应用启动时若库里没有新鲜数据，补跑一次
  - POST /api/hot_topics/research（手动触发大模型研判，立即返回，后台线程执行）

研判只针对当前榜单（每个数据源最近一次快照），历史热点不参与（HotTopicMatcher.pending）。

后台线程使用独立的 SQLite 连接（WAL 下读写并发），不与请求线程共用连接。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_STATE: dict = {
    "running": False,
    "tenant_id": None,
    "started_at": None,
    "finished_at": None,
    "trigger": None,
    "report": None,
    "match": None,
    "error": None,
    # 采集进度（stage / label / percent / sources / message / updated_at），前端轮询展示
    "progress": None,
}

# 大模型研判单独一条状态，前端轮询 research_job
_RESEARCH_STATE: dict = {
    "running": False,
    "tenant_id": None,
    "started_at": None,
    "finished_at": None,
    "report": None,
    "error": None,
}


def status() -> dict:
    with _LOCK:
        return dict(_STATE)


def research_status() -> dict:
    with _LOCK:
        return dict(_RESEARCH_STATE)


def run_once(cfg, conn=None, *, trigger: str = "manual", tenant_id: str | None = None) -> dict:
    """同步执行一次采集 + 匹配。已有任务在跑时直接返回 {"skipped": True}。

    整个过程跑在后台线程里（trigger_background），进度写进 _STATE["progress"]，
    前端切换页签 / 离开页面都不会影响采集本身。
    """
    with _LOCK:
        if _STATE["running"]:
            return {"skipped": True, **_STATE}
        _STATE.update(running=True, tenant_id=tenant_id, trigger=trigger, error=None, progress=None,
                      started_at=datetime.now().isoformat(timespec="seconds"))
    own_conn = conn is None
    if own_conn:
        from .db import Database
        conn = Database(cfg).connect()

    def on_progress(update: dict) -> None:
        with _LOCK:
            prog = dict(_STATE.get("progress") or {})
            prog.update(update)
            # 后续阶段（写入/匹配）没有新的数据源明细时，沿用抓取阶段的清单
            prog["sources"] = update.get("sources") or prog.get("sources") or []
            prog["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _STATE["progress"] = prog

    report, match, error = None, None, None
    try:
        from .hot_topics import HotTopicCollector
        from .hot_topic_matcher import HotTopicMatcher
        from .shared_raw_topics import (
            import_to_tenant, publish_from_tenant, shared_snapshot_fresh,
        )
        collector = HotTopicCollector(cfg, conn)
        # 先把当前账号组的原始字段发布到共享快照；SQL 只允许新数据覆盖旧数据。
        publish_from_tenant(conn)
        reused_shared = False
        if cfg.hot_topics.enabled is not False:
            report = HotTopicCollector(cfg, conn).refresh(on_progress=on_progress)
        on_progress({"stage": "matching", "label": "标签匹配 / 研判", "percent": 92,
                     "message": "按账号画像匹配定位关联"})
        try:
            match = HotTopicMatcher(cfg, conn).run()
        except Exception as exc:  # noqa: BLE001 — 匹配失败不回滚采集结果
            logger.exception("热点标签匹配失败")
            match = {"error": str(exc)}
        logger.info(
            "🔥 热点后台刷新完成（%s）：共享复用=%s · 采集/同步 %s · 本组匹配 %s",
            trigger, reused_shared, report, match,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("热点后台刷新失败")
        error = str(exc)
    finally:
        if own_conn:
            conn.close()
        with _LOCK:
            prog = dict(_STATE.get("progress") or {})
            collected = sum(1 for v in (report or {}).values() if v == "collected")
            prog.update({
                "stage": "failed" if error else "done",
                "label": "采集失败" if error else "采集完成",
                "percent": 100,
                "message": error or f"{collected} 个数据源采集成功",
            })
            prog["sources"] = prog.get("sources") or []
            prog["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _STATE.update(running=False, report=report, match=match, error=error,
                          progress=prog,
                          finished_at=datetime.now().isoformat(timespec="seconds"))
    return status()


def trigger_background(cfg, *, trigger: str = "manual", tenant_id: str | None = None) -> dict:
    """在后台线程启动一次刷新，立即返回当前状态。"""
    with _LOCK:
        if _STATE["running"]:
            return {"started": False, **_STATE}
    t = threading.Thread(
        target=run_once, args=(cfg,), kwargs={"trigger": trigger, "tenant_id": tenant_id},
        name=f"hot-topics-refresh-{tenant_id or 'unknown'}", daemon=True,
    )
    t.start()
    return {"started": True, **status()}


def run_research(cfg, conn=None, *, tenant_id: str | None = None) -> dict:
    """同步执行一次大模型研判（只处理当前榜单里尚未研判的条目）。"""
    with _LOCK:
        if _RESEARCH_STATE["running"]:
            return {"skipped": True, **research_status()}
        if _STATE["running"]:
            return {"skipped": True, "reason": "热点正在采集中，稍后再研判", **research_status()}
        _RESEARCH_STATE.update(running=True, tenant_id=tenant_id, error=None, report=None,
                               started_at=datetime.now().isoformat(timespec="seconds"))
    own_conn = conn is None
    if own_conn:
        from .db import Database
        conn = Database(cfg).connect()
    report, error = None, None
    try:
        from .hot_topic_matcher import HotTopicMatcher, default_llm_fn
        if default_llm_fn(cfg) is None:
            raise RuntimeError("大模型未启用：请到「系统设置」开启主模型并配置 API Key")
        report = HotTopicMatcher(cfg, conn).run()
        logger.info("🔥 热点研判完成：%s", report)
    except Exception as exc:  # noqa: BLE001
        logger.exception("热点研判失败")
        error = str(exc)
    finally:
        if own_conn:
            conn.close()
        with _LOCK:
            _RESEARCH_STATE.update(running=False, report=report, error=error,
                                   finished_at=datetime.now().isoformat(timespec="seconds"))
    return research_status()


def trigger_research(cfg, *, tenant_id: str | None = None) -> dict:
    """在后台线程启动一次大模型研判，立即返回当前状态。"""
    with _LOCK:
        if _RESEARCH_STATE["running"]:
            return {"started": False, **research_status()}
        if _STATE["running"]:
            return {"started": False, "reason": "热点正在采集中，稍后再研判", **research_status()}
    t = threading.Thread(
        target=run_research, args=(cfg,), kwargs={"tenant_id": tenant_id},
        name=f"hot-topics-research-{tenant_id or 'unknown'}", daemon=True,
    )
    t.start()
    return {"started": True, **research_status()}


def is_stale(cfg, conn) -> bool:
    from .hot_topics import HotTopicCollector
    try:
        return not HotTopicCollector(cfg, conn)._is_fresh()
    except Exception:  # noqa: BLE001
        return True


def reset_runtime_state() -> None:
    """账号组切换后清除进程级 UI 状态；只允许在采集/研判均空闲时调用。"""
    with _LOCK:
        if _STATE["running"] or _RESEARCH_STATE["running"]:
            raise RuntimeError("热点后台任务仍在执行，不能清理账号组运行状态")
        _STATE.update(running=False, tenant_id=None, started_at=None, finished_at=None,
                      trigger=None, report=None, match=None, error=None, progress=None)
        _RESEARCH_STATE.update(running=False, tenant_id=None, started_at=None, finished_at=None,
                               report=None, error=None)


def reset_for_tests() -> None:
    with _LOCK:
        _STATE.update(running=False, started_at=None, finished_at=None, trigger=None,
                      report=None, match=None, error=None, progress=None)
        _RESEARCH_STATE.update(running=False, started_at=None, finished_at=None,
                               report=None, error=None)


__all__ = ["status", "research_status", "run_once", "run_research", "trigger_background",
           "trigger_research", "is_stale", "reset_runtime_state", "reset_for_tests"]
