"""热点异步采集服务：采集 → 落库 → 标签匹配，单飞（同一时刻只跑一次）。

调用方：
  - APScheduler 定时任务 hot_topics_refresh（频率见 config scheduler.jobs.hot_topics_refresh）
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
        from .shared_raw_topics import (
            import_to_tenant, publish_from_tenant, shared_snapshot_fresh,
        )
        collector = HotTopicCollector(cfg, conn)
        # 先把当前账号组的原始字段发布到共享快照；SQL 只允许新数据覆盖旧数据。
        publish_from_tenant(conn)
        reused_shared = False
        if cfg.hot_topics.enabled is not False:
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
