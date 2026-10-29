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


def run_once(cfg, conn=None, *, trigger: str = "manual") -> dict:
    """同步执行一次采集 + 匹配。已有任务在跑时直接返回 {"skipped": True}。

    整个过程跑在后台线程里（trigger_background），进度写进 _STATE["progress"]，
    前端切换页签 / 离开页面都不会影响采集本身。
    """
    with _LOCK:
        if _STATE["running"]:
            return {"skipped": True, **_STATE}
        _STATE.update(running=True, trigger=trigger, error=None, progress=None,
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
<<<<<<< HEAD
            interval = collector.refresh_interval_minutes() or 24 * 60
            if trigger != "manual" and shared_snapshot_fresh(interval):
                # 新账号组复用原始采集，不复用其他账号组的匹配分、排序或分析结果。
                sync_result = import_to_tenant(conn)
                report = {"shared_snapshot_reused": True, "tenant_sync": sync_result}
                reused_shared = True
            else:
                report = collector.refresh()
                publish_from_tenant(conn)
        else:
            sync_result = import_to_tenant(conn)
            report = {"collection_disabled": True, "tenant_sync": sync_result}
        if reused_shared or cfg.hot_topics.enabled is False:
            # 去重、跨平台归并与保留策略属于账号组加工结果，必须在本组重新计算。
            collector.apply_dedup_and_retention(report)
=======
            report = HotTopicCollector(cfg, conn).refresh(on_progress=on_progress)
        on_progress({"stage": "matching", "label": "标签匹配 / 研判", "percent": 92,
                     "message": "按账号画像匹配定位关联"})
>>>>>>> 7f4f9e0 (chore: auth middleware localhost 放行 + auth/status 同步返回 configured=true)
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


def trigger_background(cfg, *, trigger: str = "manual") -> dict:
    """在后台线程启动一次刷新，立即返回当前状态。"""
    with _LOCK:
        if _STATE["running"]:
            return {"started": False, **_STATE}
    t = threading.Thread(target=run_once, args=(cfg,), kwargs={"trigger": trigger},
                         name="hot-topics-refresh", daemon=True)
    t.start()
    return {"started": True, **status()}


def run_research(cfg, conn=None) -> dict:
    """同步执行一次大模型研判（只处理当前榜单里尚未研判的条目）。"""
    with _LOCK:
        if _RESEARCH_STATE["running"]:
            return {"skipped": True, **research_status()}
        if _STATE["running"]:
            return {"skipped": True, "reason": "热点正在采集中，稍后再研判", **research_status()}
        _RESEARCH_STATE.update(running=True, error=None, report=None,
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


def trigger_research(cfg) -> dict:
    """在后台线程启动一次大模型研判，立即返回当前状态。"""
    with _LOCK:
        if _RESEARCH_STATE["running"]:
            return {"started": False, **research_status()}
        if _STATE["running"]:
            return {"started": False, "reason": "热点正在采集中，稍后再研判", **research_status()}
    t = threading.Thread(target=run_research, args=(cfg,), name="hot-topics-research", daemon=True)
    t.start()
    return {"started": True, **research_status()}


def is_stale(cfg, conn) -> bool:
    from .hot_topics import HotTopicCollector
    try:
        return not HotTopicCollector(cfg, conn)._is_fresh()
    except Exception:  # noqa: BLE001
        return True


def reset_for_tests() -> None:
    with _LOCK:
        _STATE.update(running=False, started_at=None, finished_at=None, trigger=None,
                      report=None, match=None, error=None, progress=None)
        _RESEARCH_STATE.update(running=False, started_at=None, finished_at=None,
                               report=None, error=None)


__all__ = ["status", "research_status", "run_once", "run_research", "trigger_background",
           "trigger_research", "is_stale", "reset_for_tests"]
