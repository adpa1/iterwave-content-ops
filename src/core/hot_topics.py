"""
热点采集 —— 60s.viki.moe 公益 API（R22）。

约束（R22）：
- 每条热点必须携带 source / discovered_at / raw_url / evidence_status
- 采集失败 → evidence_status='failed'，heat_score 保持 NULL（绝不填假数字）
- 缓存 30 分钟（config.hot_topics.refresh_interval_minutes）
- 4 道门：热度/时效可配可关；维度匹配与可发布性是安全底线（强制开启）

用法：
    from src.core.hot_topics import HotTopicCollector
    collector = HotTopicCollector(cfg, conn)
    data = collector.overview()            # 用缓存
    data = collector.overview(force=True)  # 强制刷新
"""

from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 各平台热度单位（不同平台 hot_value 口径不同，不能混算）
_HEAT_UNIT = {
    "weibo": "讨论热度",
    "douyin": "热度指数",
    "zhihu": "热度",
    "toutiao": "热度",
    "bilibili": "播放量",
}

_MAX_ITEMS_PER_PLATFORM = 30
_FETCH_TIMEOUT_SECONDS = 15

# 配置里的平台名 → 60s API 路径名（bilibili 的接口路径是 bili）
_PLATFORM_PATH = {"bilibili": "bili"}
_MULTIPLIERS = {"万": 10_000, "亿": 100_000_000}


def _topic_id(platform: str, title: str) -> str:
    raw = f"{platform}|{title}".encode("utf-8")
    return f"hot-{hashlib.sha1(raw).hexdigest()[:16]}"


def _parse_heat(value, desc: Optional[str]) -> Optional[float]:
    """热度数字：优先 hot_value；知乎只有「17807 万热度」这类文案 → 解析成数字。"""
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    if not desc:
        return None
    m = re.search(r"([\d.]+)\s*(万|亿)?", str(desc))
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    return num * _MULTIPLIERS.get(m.group(2) or "", 1)


def _parse_event_ts(raw: dict) -> Optional[datetime]:
    """事件时间戳：秒 / 毫秒都兼容；解析不到就返回 None（未知 ≠ 0，R3）。"""
    for key in ("event_time_at", "created_at"):
        ts = raw.get(key)
        if not isinstance(ts, (int, float)):
            continue
        try:
            seconds = ts / 1000 if ts > 10_000_000_000 else ts
            return datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            continue
    return None


class HotTopicCollector:
    """热点采集器 — 按 profile 抓取、缓存、落 hot_topics 表。"""

    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        self.hcfg = cfg.hot_topics
        self.source = self.hcfg.sources[0] if self.hcfg.sources else {}
        self.base_url = (self.source.get("url") or "https://60s.viki.moe").rstrip("/")
        self.platforms = list(self.source.get("platforms") or ["weibo", "douyin"])
        self.source_name = self.source.get("name") or "60s.viki.moe"

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def overview(self, force: bool = False) -> dict:
        """返回热点雷达数据（默认命中 30min 缓存）。"""
        if self.hcfg.enabled is False:
            return {
                "enabled": False,
                "cached": False,
                "refreshed_at": None,
                "sources": [],
                "items": [],
                "message": "热点采集已在配置中关闭（hot_topics.enabled=false）",
            }

        refreshed = False
        if force or not self._is_fresh():
            self.refresh()
            refreshed = True

        items, sources = self._load()
        return {
            "enabled": True,
            "cached": not refreshed,
            "refreshed_at": self._last_collected_at(),
            "refresh_interval_minutes": self.hcfg.refresh_interval_minutes,
            "sources": sources,
            "items": items,
            "filters": {
                "heat_threshold": self.hcfg.filters.get("heat_threshold"),
                "heat_filter_enabled": self.hcfg.filters.get("heat_filter_enabled", True),
                "time_window_hours": self.hcfg.filters.get("time_window_hours"),
                "time_filter_enabled": self.hcfg.filters.get("time_filter_enabled", True),
                "dimension_match_required": True,   # R22 强制
                "publishability_required": True,    # R22 强制
            },
        }

    def refresh(self) -> dict:
        """并发抓取所有配置平台 → 落库。返回各平台采集结果。"""
        report: dict[str, str] = {}
        # 顺序抓 5 个平台 × 15s 超时会把接口拖到 1 分钟以上，改并发
        with ThreadPoolExecutor(max_workers=max(1, len(self.platforms))) as pool:
            futures = {
                platform: pool.submit(self._fetch_platform, platform)
                for platform in self.platforms
            }
            results: dict[str, list[dict] | Exception] = {}
            for platform, fut in futures.items():
                try:
                    results[platform] = fut.result()
                except Exception as exc:  # noqa: BLE001 — 单平台失败不影响其他平台
                    results[platform] = exc

        for platform, result in results.items():
            if isinstance(result, Exception):
                logger.warning("热点采集失败 %s: %s", platform, result)
                self._record_failure(platform, str(result))
                report[platform] = "failed"
                continue
            if not result:
                self._record_failure(platform, "接口返回空列表")
                report[platform] = "failed"
                continue

            self._upsert(platform, result)
            report[platform] = "collected"
            logger.info("🔥 热点采集成功 %s：%d 条", platform, len(result))

        return report

    # ------------------------------------------------------------------
    # 抓取
    # ------------------------------------------------------------------

    def _fetch_platform(self, platform: str) -> list[dict]:
        path = _PLATFORM_PATH.get(platform, platform)
        url = f"{self.base_url}/v2/{path}"
        resp = httpx.get(
            url, timeout=_FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": "iterwave/0.1 (+local)"},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") not in (200, None):
            raise RuntimeError(f"接口返回 code={payload.get('code')} {payload.get('message', '')}")

        window_h = self.hcfg.filters.get("time_window_hours")
        now = datetime.now()
        out: list[dict] = []
        for raw in (payload.get("data") or [])[:_MAX_ITEMS_PER_PLATFORM]:
            title = (raw.get("title") or "").strip()
            if not title:
                continue
            heat = _parse_heat(raw.get("hot_value"), raw.get("hot_value_desc"))

            # 时效：能解析出事件时间才判断，解析不到就是未知（NULL ≠ 0，R3）
            time_sensitive: Optional[int] = None
            event_at = _parse_event_ts(raw)
            if event_at is not None and window_h:
                time_sensitive = 1 if (now - event_at) <= timedelta(hours=float(window_h)) else 0

            out.append({
                "id": _topic_id(platform, title),
                "title": title,
                "raw_url": raw.get("link") or raw.get("url"),
                "heat_score": heat,
                "heat_unit": _HEAT_UNIT.get(platform, "热度"),
                "time_sensitive": time_sensitive,
            })
        return out

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    def _upsert(self, platform: str, rows: list[dict]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        source = f"{self.source_name}/{platform}"
        for r in rows:
            self.conn.execute(
                "INSERT INTO hot_topics (id, source, raw_url, discovered_at, evidence_status, "
                "title, heat_score, heat_unit, time_sensitive, dimension) "
                "VALUES (?, ?, ?, ?, 'collected', ?, ?, ?, ?, NULL) "
                "ON CONFLICT(id) DO UPDATE SET "
                "source=excluded.source, raw_url=excluded.raw_url, "
                "discovered_at=excluded.discovered_at, evidence_status='collected', "
                "heat_score=excluded.heat_score, heat_unit=excluded.heat_unit, "
                "time_sensitive=excluded.time_sensitive",
                (
                    r["id"], source, r["raw_url"], now, r["title"],
                    r["heat_score"], r["heat_unit"], r["time_sensitive"],
                ),
            )
        # 该平台之前的失败占位行清理掉
        self.conn.execute(
            "DELETE FROM hot_topics WHERE source = ? AND evidence_status = 'failed'",
            (source,),
        )
        self.conn.commit()

    def _record_failure(self, platform: str, reason: str) -> None:
        """采集失败：落一行 failed 占位（heat_score = NULL，不显示数字）。"""
        source = f"{self.source_name}/{platform}"
        try:
            self.conn.execute(
                "INSERT INTO hot_topics (id, source, raw_url, discovered_at, evidence_status, "
                "title, heat_score, heat_unit, time_sensitive, dimension) "
                "VALUES (?, ?, NULL, ?, 'failed', ?, NULL, NULL, NULL, NULL) "
                "ON CONFLICT(id) DO UPDATE SET "
                "discovered_at=excluded.discovered_at, evidence_status='failed', "
                "heat_score=NULL, raw_url=NULL",
                (
                    f"hot-failed-{platform}", source,
                    datetime.now().isoformat(timespec="seconds"),
                    f"{platform} 热点采集失败：{reason[:100]}",
                ),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001 — 失败留痕本身不能再抛
            logger.exception("热点失败占位落库失败 platform=%s", platform)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _is_fresh(self) -> bool:
        last = self._last_collected_at()
        if not last:
            return False
        try:
            dt = datetime.fromisoformat(last)
        except ValueError:
            return False
        return (datetime.now() - dt) < timedelta(minutes=self.hcfg.refresh_interval_minutes)

    def _last_collected_at(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(discovered_at) AS ts FROM hot_topics WHERE evidence_status != 'demo'"
        ).fetchone()
        return row["ts"] if row and row["ts"] else None

    def _load(self) -> tuple[list[dict], list[dict]]:
        """读取缓存 → (通过门的条目, 各平台数据源状态)。"""
        rows = self.conn.execute(
            "SELECT * FROM hot_topics ORDER BY heat_score DESC NULLS LAST"
        ).fetchall()

        filters = self.hcfg.filters
        heat_on = filters.get("heat_filter_enabled", True)
        heat_min = filters.get("heat_threshold") or 0
        time_on = filters.get("time_filter_enabled", True)

        redlines = [k for k in self.cfg.ultimate_goal.redline_keywords if k]
        personas = [k for k in self.cfg.ultimate_goal.persona_keywords if k]

        items: list[dict] = []
        sources: dict[str, dict] = {}
        failed: dict[str, dict] = {}

        # 第一遍：找出「最近一次采集失败」的平台，其旧数据一并隐藏（避免失败还显示数字）
        for row in rows:
            if row["evidence_status"] != "failed":
                continue
            source = row["source"] or ""
            platform = source.rsplit("/", 1)[-1] if "/" in source else source
            prev = failed.get(platform)
            if prev is None or (row["discovered_at"] or "") > (prev["discovered_at"] or ""):
                failed[platform] = {
                    "platform": platform,
                    "source": source,
                    "status": "failed",
                    "message": row["title"],
                    "discovered_at": row["discovered_at"],
                }

        # 第二遍：只保留采集成功的平台条目，并过 4 道门
        for row in rows:
            source = row["source"] or ""
            platform = source.rsplit("/", 1)[-1] if "/" in source else source
            if row["evidence_status"] == "failed":
                continue
            if platform in failed:
                continue  # 该平台最近一次采集失败 → 不展示旧条目

            sources.setdefault(platform, {
                "platform": platform,
                "source": source,
                "status": row["evidence_status"],
                "message": None,
                "discovered_at": row["discovered_at"],
            })

            heat = row["heat_score"]
            # 🔥 热度门（可关）：有数字才判，NULL 不排除
            if heat_on and heat is not None and heat < float(heat_min):
                continue
            # ⏰ 时效门（可关）：未知不排除（R3）
            if time_on and row["time_sensitive"] is not None and row["time_sensitive"] == 0:
                continue

            title = row["title"] or ""
            hit_redline = [k for k in redlines if k in title]
            hit_persona = [k for k in personas if k in title]

            items.append({
                "id": row["id"],
                "title": title,
                "source": source,
                "discovered_at": row["discovered_at"],
                "url": row["raw_url"],
                "heat": heat,
                "heat_unit": row["heat_unit"],
                "time_sensitive": row["time_sensitive"],
                "evidence_status": row["evidence_status"],
                "dimension": row["dimension"],
                # 🎯 维度匹配（强制门）：关键词命中情况，供前端/选题环节判断
                "dimension_match": (True if hit_persona else None),
                "dimension_hits": hit_persona,
                # ✅ 可发布性（强制门）：命中红线 → 不可用
                "publishable": not hit_redline,
                "redline_hits": hit_redline,
            })

        return items, list(sources.values()) + list(failed.values())
