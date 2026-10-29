"""
选题池 — 生产、评分、Web 展示共用同一份选题来源。

来源：
1. 本地选题库文件（KbReader，config.topic_kb）
2. topic_index 里的非 KB 来源：热点转入（source = hot:*）、手动录入（source = manual）

软删除：topic_index.excluded_at 非空的选题从选题池剔除（含 KB 文件里的选题——
KB 文件是用户自己的数据，系统不改它，只在本地记一笔"剔除"），可恢复。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Optional

from .kb_sources import read_all
from .scoring.topic_eval import TopicItem

logger = logging.getLogger(__name__)

MANUAL_SOURCE = "manual"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def excluded_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT id FROM topic_index WHERE excluded_at IS NOT NULL")}


def load_pool(cfg, conn, *, include_excluded: bool = False) -> tuple[list[TopicItem], dict]:
    """返回 (选题列表, 报告)。KB 读失败不抛出，写进报告，仍返回非 KB 来源的选题。"""
    report: dict = {"kb_count": 0, "extra_count": 0, "excluded_count": 0, "kb_error": None, "file_type": None}
    items: list[TopicItem] = []
    seen: set[str] = set()
    try:
        kb_items, kb_report = read_all(cfg)
        report["file_type"] = kb_report.get("file_type")
        report["sources"] = kb_report.get("sources", [])
        for it in kb_items:
            if it.id in seen:
                continue
            seen.add(it.id)
            it.source = it.source or "kb"
            items.append(it)
        report["kb_count"] = len(items)
        report["kb_error"] = kb_report.get("kb_error")
    except Exception as exc:  # noqa: BLE001 — KB 不可读也要让其它来源可用，并把原因暴露给前端
        report["kb_error"] = str(exc)

    rows = conn.execute(
        "SELECT id, title, dimension, source, date, summary, link FROM topic_index "
        "WHERE source = ? OR source LIKE 'hot:%'",
        (MANUAL_SOURCE,),
    ).fetchall()
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        items.append(TopicItem(
            id=r[0], title=r[1] or "", dimension=r[2] or "", source=r[3] or "",
            date=r[4], summary=r[5] or "", link=r[6] or "",
        ))
        report["extra_count"] += 1

    excluded = excluded_ids(conn)
    report["excluded_count"] = len(excluded & seen)
    if not include_excluded:
        items = [it for it in items if it.id not in excluded]
    return items, report


def list_topics(cfg, conn, *, q: str = "", dimension: str = "", source: str = "",
                status: str = "active", limit: int = 50, offset: int = 0) -> dict:
    """Web 选题池列表：合并来源 + 使用次数 + 是否剔除，支持筛选分页。"""
    items, report = load_pool(cfg, conn, include_excluded=True)
    meta = {
        r[0]: {"total_uses": r[1] or 0, "excluded_at": r[2], "excluded_reason": r[3],
               "avg_interaction_rate": r[4]}
        for r in conn.execute(
            "SELECT id, total_uses, excluded_at, excluded_reason, avg_interaction_rate FROM topic_index"
        )
    }
    used = {r[0]: r[1] for r in conn.execute(
        "SELECT topic_id, COUNT(*) FROM contents GROUP BY topic_id"
    )}
    rows = []
    for it in items:
        m = meta.get(it.id, {})
        excluded = bool(m.get("excluded_at"))
        src = it.source or "kb"
        kind = "hot" if src.startswith("hot:") else ("manual" if src == MANUAL_SOURCE else "kb")
        rows.append({
            "id": it.id, "title": it.title, "dimension": it.dimension, "summary": it.summary,
            "link": it.link, "date": it.date, "source": src, "source_kind": kind,
            "content_count": used.get(it.id, 0), "excluded": excluded,
            "excluded_at": m.get("excluded_at"), "excluded_reason": m.get("excluded_reason"),
        })
    dims = sorted({r["dimension"] for r in rows if r["dimension"]})
    if status == "active":
        rows = [r for r in rows if not r["excluded"]]
    elif status == "excluded":
        rows = [r for r in rows if r["excluded"]]
    if dimension:
        rows = [r for r in rows if r["dimension"] == dimension]
    if source:
        rows = [r for r in rows if r["source_kind"] == source]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r["title"] or "").lower() or ql in (r["summary"] or "").lower()]
    total = len(rows)
    return {"items": rows[offset: offset + limit], "total": total, "dimensions": dims, "report": report}


def add_manual_topic(conn, *, title: str, dimension: str, summary: str = "", link: str = "") -> dict:
    title = (title or "").strip()
    dimension = (dimension or "").strip()
    if len(title) < 4:
        raise ValueError("标题至少 4 个字")
    if not dimension:
        raise ValueError("必须填写内容方向（维度），否则无法评分与做维度分配")
    topic_id = "manual-" + hashlib.sha1(f"{dimension}|{title}".encode("utf-8")).hexdigest()[:12]
    row = conn.execute("SELECT excluded_at FROM topic_index WHERE id = ?", (topic_id,)).fetchone()
    if row is not None:
        if row[0]:
            conn.execute("UPDATE topic_index SET excluded_at = NULL, excluded_reason = NULL WHERE id = ?", (topic_id,))
            conn.commit()
            return {"id": topic_id, "created": False, "restored": True}
        return {"id": topic_id, "created": False, "restored": False}
    conn.execute(
        "INSERT INTO topic_index (id, title, dimension, source, date, summary, link, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (topic_id, title, dimension, MANUAL_SOURCE, datetime.now().strftime("%Y-%m-%d"),
         summary.strip() or None, link.strip() or None, _now()),
    )
    conn.commit()
    return {"id": topic_id, "created": True, "restored": False}


def exclude_topics(cfg, conn, ids: list[str], reason: str = "") -> dict:
    """软删除：KB 文件里的选题在 topic_index 记一行剔除标记；手动/热点选题同样只打标，可恢复。"""
    ids = [i for i in dict.fromkeys(str(x).strip() for x in ids) if i]
    if not ids:
        raise ValueError("ids 不能为空")
    pool = {it.id: it for it in load_pool(cfg, conn, include_excluded=True)[0]}
    missing = [i for i in ids if i not in pool]
    now = _now()
    done = []
    for i in ids:
        if i in missing:
            continue
        it = pool[i]
        conn.execute(
            "INSERT INTO topic_index (id, title, dimension, source, date, summary, link, first_seen_at, "
            "excluded_at, excluded_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET excluded_at = excluded.excluded_at, "
            "excluded_reason = excluded.excluded_reason",
            (i, it.title, it.dimension, it.source or "kb", it.date, it.summary or None, it.link or None,
             now, now, reason.strip() or "用户删除"),
        )
        done.append(i)
    conn.commit()
    return {"excluded": done, "not_found": missing}


def restore_topics(conn, ids: list[str]) -> dict:
    ids = [i for i in dict.fromkeys(str(x).strip() for x in ids) if i]
    cur = conn.executemany(
        "UPDATE topic_index SET excluded_at = NULL, excluded_reason = NULL WHERE id = ?",
        [(i,) for i in ids],
    )
    conn.commit()
    return {"restored": ids, "count": cur.rowcount}


def purge_topics(conn, ids: list[str]) -> dict:
    """彻底删除手动/热点选题（KB 文件选题只能剔除，不能物理删除；已产出内容的选题保留以免断链）。"""
    deleted, kept = [], []
    for i in dict.fromkeys(ids):
        row = conn.execute("SELECT source FROM topic_index WHERE id = ?", (i,)).fetchone()
        used = conn.execute("SELECT COUNT(*) FROM contents WHERE topic_id = ?", (i,)).fetchone()[0]
        src = (row[0] or "") if row else ""
        if row is None or used or not (src == MANUAL_SOURCE or src.startswith("hot:")):
            kept.append(i)
            continue
        conn.execute("DELETE FROM topic_index WHERE id = ?", (i,))
        deleted.append(i)
    conn.commit()
    return {"deleted": deleted, "kept": kept}
