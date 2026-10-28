"""热点去重与保留规则（采集入库时执行，防止定时任务把库塞满）。

规则 1（同平台去重）：标题归一化后相同 → 同一行。
    归一化 = 全角转半角（NFKC）→ 英文小写 → 去掉空白、标点、符号、emoji
    行 id = hash(平台|归一化标题)，重复采集只更新热度 / last_seen_at / seen_count，不新增行。
规则 2（跨平台合并）：在 cross_platform_window_hours 时间窗内，归一化标题相同，
    或字符二元组 Jaccard 相似度 ≥ cross_platform_similarity → 视为同一事件。
    主条目 = 最早发现的那条（稳定，不会因热度波动来回切换），其余 duplicate_of 指向主条目。
    雷达只展示主条目（附「同时在榜」平台），标签匹配也只对主条目做。
规则 3（保留）：last_seen_at 早于 retention_days 的删除；总量超过 max_rows 时从最久未在榜的开始删。
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    # \W 会保留 CJK 与字母数字，去掉标点 / 空白 / emoji 等符号
    t = "".join(ch for ch in t if not unicodedata.category(ch).startswith(("S", "P", "Z", "C")))
    return _STRIP.sub("", t)


def topic_id(platform: str, title: str) -> str:
    raw = f"{platform}|{normalize_title(title) or title}".encode("utf-8")
    return f"hot-{hashlib.sha1(raw).hexdigest()[:16]}"


def _bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ba, bb = _bigrams(a), _bigrams(b)
    return len(ba & bb) / len(ba | bb)


def backfill(conn) -> int:
    """迁移前的旧行补 platform / title_norm，并按规则 1 合并历史重复（保留最近在榜的一行）。"""
    rows = conn.execute(
        "SELECT id, source, title, discovered_at, last_seen_at FROM hot_topics "
        "WHERE evidence_status = 'collected' AND (title_norm IS NULL OR platform IS NULL)"
    ).fetchall()
    for r in rows:
        source = r["source"] or ""
        platform = source.rsplit("/", 1)[-1] if "/" in source else source
        conn.execute("UPDATE hot_topics SET platform = ?, title_norm = ? WHERE id = ?",
                     (platform, normalize_title(r["title"] or ""), r["id"]))
    merged = 0
    groups = conn.execute(
        "SELECT platform, title_norm, COUNT(*) AS n FROM hot_topics WHERE evidence_status = 'collected' "
        "AND title_norm IS NOT NULL GROUP BY platform, title_norm HAVING n > 1"
    ).fetchall()
    for g in groups:
        dup = conn.execute(
            "SELECT id, discovered_at, seen_count FROM hot_topics WHERE platform = ? AND title_norm = ? "
            "AND evidence_status = 'collected' ORDER BY last_seen_at DESC",
            (g["platform"], g["title_norm"]),
        ).fetchall()
        keep, rest = dup[0], dup[1:]
        first_seen = min(d["discovered_at"] or "" for d in dup) or keep["discovered_at"]
        seen = sum(d["seen_count"] or 1 for d in dup)
        conn.execute("UPDATE hot_topics SET discovered_at = ?, seen_count = ? WHERE id = ?",
                     (first_seen, seen, keep["id"]))
        conn.executemany("DELETE FROM hot_topics WHERE id = ?", [(d["id"],) for d in rest])
        merged += len(rest)
    if rows or merged:
        conn.commit()
    return merged


def mark_cross_platform(conn, *, threshold: float, window_hours: int) -> int:
    """规则 2：时间窗内跨平台同一事件 → duplicate_of 主条目。返回被标记为重复的行数。"""
    since = (datetime.now() - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, platform, title_norm, discovered_at FROM hot_topics "
        "WHERE evidence_status = 'collected' AND title_norm IS NOT NULL AND title_norm != '' "
        "AND last_seen_at >= ? ORDER BY discovered_at ASC, id ASC",
        (since,),
    ).fetchall()
    canon: list[dict] = []          # 主条目（按首次发现先后）
    assign: dict[str, str | None] = {}
    for r in rows:
        norm = r["title_norm"]
        target = None
        for c in canon:
            if c["platform"] == r["platform"]:
                continue        # 同平台已由规则 1 去重，不同标题视为不同话题
            if abs(len(c["title_norm"]) - len(norm)) > max(len(norm), len(c["title_norm"])) * 0.5:
                continue
            if similarity(c["title_norm"], norm) >= threshold:
                target = c["id"]
                break
        assign[r["id"]] = target
        if target is None:
            canon.append(dict(r))
    conn.executemany("UPDATE hot_topics SET duplicate_of = ? WHERE id = ?",
                     [(v, k) for k, v in assign.items()])
    conn.commit()
    return sum(1 for v in assign.values() if v)


def prune(conn, *, retention_days: int, max_rows: int) -> int:
    """规则 3：按保留天数与总量上限清理。返回删除行数。"""
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat(timespec="seconds")
    deleted = conn.execute(
        "DELETE FROM hot_topics WHERE COALESCE(last_seen_at, discovered_at) < ?", (cutoff,)
    ).rowcount
    total = conn.execute("SELECT COUNT(*) FROM hot_topics").fetchone()[0]
    if total > max_rows:
        deleted += conn.execute(
            "DELETE FROM hot_topics WHERE id IN (SELECT id FROM hot_topics "
            "ORDER BY COALESCE(last_seen_at, discovered_at) ASC LIMIT ?)",
            (total - max_rows,),
        ).rowcount
    # 主条目被删后，残留的 duplicate_of 指针清空（下一轮重新判定）
    conn.execute(
        "UPDATE hot_topics SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL "
        "AND duplicate_of NOT IN (SELECT id FROM hot_topics)"
    )
    conn.commit()
    if deleted:
        logger.info("🧹 热点保留清理：删除 %d 行（保留 %d 天，上限 %d 行）", deleted, retention_days, max_rows)
    return deleted
