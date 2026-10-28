"""热点异步采集服务：采集 → 落库 → 标签匹配，单飞（同一时刻只跑一次）。

调用方：
  - APScheduler 定时任务 hot_topics_refresh（每 refresh_interval_minutes 分钟）
  - POST /api/hot_topics/refresh（手动触发，立即返回，后台线程执行）
  - 应用启动时若库里没有新鲜数据，补跑一次

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
}


def status() -> dict:
    with _LOCK:
        return dict(_STATE)


def run_once(cfg, conn=None, *, trigger: str = "manual") -> dict:
    """同步执行一次采集 + 匹配。已有任务在跑时直接返回 {"skipped": True}。"""
    with _LOCK:
        if _STATE["running"]:
            return {"skipped": True, **_STATE}
        _STATE.update(running=True, trigger=trigger, error=None,
                      started_at=datetime.now().isoformat(timespec="seconds"))
    own_conn = conn is None
    if own_conn:
        from .db import Database
        conn = Database(cfg).connect()
    report, match, error = None, None, None
    try:
        from .hot_topics import HotTopicCollector
        from .hot_topic_matcher import HotTopicMatcher
        if cfg.hot_topics.enabled is not False:
            report = HotTopicCollector(cfg, conn).refresh()
        try:
            match = HotTopicMatcher(cfg, conn).run()
        except Exception as exc:  # noqa: BLE001 — 匹配失败不回滚采集结果
            logger.exception("热点标签匹配失败")
            match = {"error": str(exc)}
        logger.info("🔥 热点后台刷新完成（%s）：采集 %s · 匹配 %s", trigger, report, match)
    except Exception as exc:  # noqa: BLE001
        logger.exception("热点后台刷新失败")
        error = str(exc)
    finally:
        if own_conn:
            conn.close()
        with _LOCK:
            _STATE.update(running=False, report=report, match=match, error=error,
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


def is_stale(cfg, conn) -> bool:
    from .hot_topics import HotTopicCollector
    try:
        return not HotTopicCollector(cfg, conn)._is_fresh()
    except Exception:  # noqa: BLE001
        return True


def reset_for_tests() -> None:
    with _LOCK:
        _STATE.update(running=False, started_at=None, finished_at=None, trigger=None,
                      report=None, match=None, error=None)


__all__ = ["status", "run_once", "trigger_background", "is_stale", "reset_for_tests"]
