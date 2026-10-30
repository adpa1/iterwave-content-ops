"""
热点采集 —— 60s.viki.moe 公益 API（R22）。

约束（R22）：
- 每条热点必须携带 source / discovered_at / raw_url / evidence_status
- 采集失败 → evidence_status='failed'，heat_score 保持 NULL（绝不填假数字）
- 采集频率：config scheduler.jobs.hot_topics_refresh；去重与保留：hot_topic_dedup
- 4 道门：热度/时效可配可关；维度匹配与可发布性是安全底线（强制开启）

采集改为异步（hot_topic_service）：
- 定时任务 hot_topics_refresh 按配置频率采集 → 落库 → 标签匹配（hot_topic_matcher）
- GET /api/hot_topics 只读库（overview()），不再在请求里同步抓取
- POST /api/hot_topics/refresh 手动触发一次后台采集，立即返回

用法：
    from src.core.hot_topics import HotTopicCollector
    collector = HotTopicCollector(cfg, conn)
    data = collector.overview()   # 只读库
    collector.refresh()           # 采集落库（由后台任务调用）
"""

from __future__ import annotations

import json
import statistics
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable, Optional


from .redact import redact

logger = logging.getLogger(__name__)

# 各平台热度单位（不同平台 hot_value 口径不同，不能混算）
_HEAT_UNIT = {
    "weibo": "讨论热度",
    "douyin": "热度指数",
    "zhihu": "热度",
    "toutiao": "热度",
    "bilibili": "播放量",
}


# 配置里的平台名 → 60s API 路径名（bilibili 的接口路径是 bili）
_PLATFORM_PATH = {"bilibili": "bili"}
_MULTIPLIERS = {"万": 10_000, "亿": 100_000_000}


def _fetch_public_text(url: str, *, headers: dict[str, str], timeout: int) -> tuple[str, str]:
    """通过固定到已验证公网 IP 的连接抓取热点源。"""
    from .kb_sources import MAX_UPLOAD_BYTES
    from .safe_http import fetch_public_bytes

    body, content_type, _final_url = fetch_public_bytes(
        url,
        headers=headers,
        timeout=timeout,
        max_bytes=MAX_UPLOAD_BYTES,
        max_redirects=5,
    )
    return body.decode("utf-8", errors="replace"), content_type


def _parse_feed_ts(value) -> Optional[datetime]:
    """RSS / JSON 行业源的发布时间：ISO / RFC822 都兼容；解析不到 → None。"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return _parse_event_ts({"event_time_at": value})
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text)
        return dt.replace(tzinfo=None) if dt else None
    except (TypeError, ValueError):
        return None


def _topic_id(platform: str, title: str) -> str:
    """去重规则 1：同平台 + 归一化标题 → 同一 id（见 hot_topic_dedup）。"""
    from .hot_topic_dedup import topic_id
    return topic_id(platform, title)


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


def _trend_of(it: dict, median: float | None, policy=None) -> tuple[str, str]:
    """趋势辅助标签：爆发 / 持续爆发 / 降温中 / 已降温 / 平稳。

    只用库里的真实数据判断：没有上轮数据（seen_count=1）绝不编「降温」，热度为 NULL 不判（R3）。
    """
    if policy is None:
        from .config import HotTopicsConfig
        policy = HotTopicsConfig()
    heat, prev, peak = it["heat"], it["heat_prev"], it["heat_peak"]
    seen = it["seen_count"] or 1
    if heat is None:
        return "平稳", "热度暂无数字，无法判断趋势"
    peak = peak or heat
    if not peak or peak <= 0:
        return "平稳", "缺少峰值数据"
    if seen <= 1:
        if median and heat >= median * policy.trend_burst_multiplier:
            return "爆发", f"首次上榜，热度是同源中位数的 {heat / median:.1f} 倍"
        return "平稳", "首次上榜，热度未明显高于同源中位数"
    if seen >= policy.trend_min_seen_count and heat <= peak * policy.trend_cooled_ratio:
        return "已降温", f"较峰值 {peak:.0f} 已回落 {(1 - heat / peak) * 100:.0f}%"
    if seen >= policy.trend_min_seen_count and heat >= peak * policy.trend_sustained_ratio:
        return "持续爆发", f"连续 {seen} 轮在榜，热度仍在峰值 {policy.trend_sustained_ratio:.0%} 以上"
    if prev is not None and prev > 0 and heat <= prev * policy.trend_previous_drop_ratio:
        return "降温中", f"较上轮 {prev:.0f} 下降 {(1 - heat / prev) * 100:.0f}%"
    return "平稳", "热度与前几轮基本持平"


class HotTopicCollector:
    """热点采集器 — 按 profile 抓取、缓存、落 hot_topics 表。"""

    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        self.hcfg = cfg.hot_topics
        configured = [s for s in (self.hcfg.sources or []) if s.get("enabled", True) is not False]
        # 来源类型：aggregate（综合热榜，默认）/ rss / json（自定义行业源）
        self.aggregate_sources = [s for s in configured if s.get("type", "aggregate") == "aggregate"]
        self.custom_sources = [s for s in configured if s.get("type") in ("rss", "json")]
        # 数据源唯一默认定义在 config/config.yaml；显式清空来源即停止采集，不再静默回退到硬编码公网源。
        self.source = self.aggregate_sources[0] if self.aggregate_sources else {}
        self.base_url = (self.source.get("url") or "").rstrip("/")
        self.platforms = list(self.source.get("platforms") or [])
        self.source_name = self.source.get("name") or ""

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def overview(self, force: bool = False) -> dict:
        """返回热点雷达数据（只读库）。force 参数保留兼容，后台刷新由调用方触发。"""
        if self.hcfg.enabled is False:
            return {
                "enabled": False,
                "cached": False,
                "refreshed_at": None,
                "sources": [],
                "items": [],
                "message": "热点采集已在配置中关闭（hot_topics.enabled=false）",
            }

        items, sources = self._load()
        last = self._last_collected_at()
        return {
            "enabled": True,
            "cached": True,
            "refreshed_at": last,
            "stale": not self._is_fresh(),
            "refresh_interval_minutes": self.cfg.scheduler.job("hot_topics_refresh").interval_minutes,
            "refresh_schedule": self.cfg.scheduler.job("hot_topics_refresh").describe(),
            "refresh_enabled": self.cfg.scheduler.job("hot_topics_refresh").enabled,
            "storage": {
                "rows": self.conn.execute("SELECT COUNT(*) FROM hot_topics").fetchone()[0],
                "retention_days": self.hcfg.retention_days,
                "max_rows": self.hcfg.max_rows,
            },
            "sources": sources,
            "items": items,
            "match": self._match_summary(items),
            "filters": {
                "heat_threshold": self.hcfg.filters.get("heat_threshold"),
                "heat_filter_enabled": self.hcfg.filters.get("heat_filter_enabled", True),
                "time_window_hours": self.hcfg.filters.get("time_window_hours"),
                "time_filter_enabled": self.hcfg.filters.get("time_filter_enabled", True),
                "dimension_match_required": True,   # R22 强制
                "publishability_required": True,    # R22 强制
            },
        }

    def _match_summary(self, items: list[dict]) -> dict:
        threshold = self.cfg.hot_topic_matcher.match_threshold
        return {
            "threshold": threshold,
            "matched": sum(1 for i in items if i["dimension_match"] is True),
            "rejected": sum(1 for i in items if i["dimension_match"] is False),
            "pending": sum(1 for i in items if i["dimension_match"] is None),
            "method": next((i["match_method"] for i in items if i.get("match_method")), None),
        }

    def refresh(self, on_progress: Optional[Callable[[dict], None]] = None) -> dict:
        """并发抓取综合热榜平台 + 自定义 RSS/JSON 行业源 → 落库。返回各来源采集结果。

        on_progress：进度回调（stage / label / percent / sources / message）。
        采集全程在后台线程执行，进度只写入状态，与前端页面是否停留在本页无关。
        """
        report: dict[str, str] = {}
        # 综合热榜：顺序抓 5 个平台 × 15s 超时会把接口拖到 1 分钟以上，改并发
        jobs: list[tuple[str, str, object]] = []   # (报告键, 展示来源, 可调用)
        for agg in self.aggregate_sources:
            base = (agg.get("url") or "https://60s.viki.moe").rstrip("/")
            name = agg.get("name") or "60s.viki.moe"
            for platform in (agg.get("platforms") or ["weibo", "douyin"]):
                jobs.append((f"{name}/{platform}", platform,
                             lambda p=platform, b=base, n=name: self._fetch_platform(p, b, n)))
        for src in self.custom_sources:
            label = src.get("platform") or src.get("name") or "行业源"
            jobs.append((label, label, lambda s=src, l=label: self._fetch_custom(s, l)))

        total = len(jobs)
        src_state: list[dict] = [
            {"key": key, "label": label, "status": "pending", "items": 0}
            for key, label, _fn in jobs
        ]

        def emit(stage: str, label: str, percent: int, *, message: Optional[str] = None) -> None:
            if on_progress is None:
                return
            on_progress({"stage": stage, "label": label, "percent": percent,
                         "sources": [dict(s) for s in src_state], "message": message})

        emit("fetching", "抓取平台热榜", 5, message=f"共 {total} 个数据源")

        with ThreadPoolExecutor(max_workers=min(self.hcfg.max_concurrent_fetches, max(1, total))) as pool:
            futures = {pool.submit(fn): (key, label) for key, label, fn in jobs}
            results: dict[str, tuple[str, list[dict] | Exception]] = {}
            done = 0
            for fut in as_completed(futures):
                key, label = futures[fut]
                try:
                    results[key] = (label, fut.result())
                except Exception as exc:  # noqa: BLE001 — 单来源失败不影响其他来源
                    results[key] = (label, exc)
                done += 1
                state = next(s for s in src_state if s["key"] == key)
                payload = results[key][1]
                if isinstance(payload, Exception) or not payload:
                    state["status"] = "failed"
                else:
                    state["status"] = "collected"
                    state["items"] = len(payload)
                emit("fetching", "抓取平台热榜", 5 + int(55 * done / max(1, total)),
                     message=f"已完成 {done}/{total} 个数据源")

        saved = 0
        for key, (label, result) in results.items():
            if isinstance(result, Exception):
                safe_key = redact(str(key))
                safe_error = redact(str(result))
                logger.warning("热点采集失败 %s: %s", safe_key, safe_error)
                self._record_failure(key, label, safe_error)
                report[key] = "failed"
            elif not result:
                self._record_failure(key, label, "接口返回空列表")
                report[key] = "failed"
            else:
                self._upsert(key, label, result)
                report[key] = "collected"
                logger.info("🔥 热点采集成功 %s：%d 条", key, len(result))
            saved += 1
            emit("saving", "写入热点库", 60 + int(25 * saved / max(1, len(results))),
                 message=f"已写入 {saved}/{len(results)} 个数据源")

        self.apply_dedup_and_retention(report)
        emit("dedup", "去重与旧热点清理", 90, message="跨平台合并 + 旧热点滚动清理完成")
        return report

    def apply_dedup_and_retention(self, report: Optional[dict] = None) -> dict:
        """去重规则 2（跨平台合并）+ 规则 3（保留清理）。采集后调用，也可单独调用。"""
        from .hot_topic_dedup import backfill, mark_cross_platform, prune
        d = self.hcfg.dedup
        stats = {
            "merged_legacy": backfill(self.conn),
            "cross_platform_duplicates": mark_cross_platform(
                self.conn, threshold=d.cross_platform_similarity,
                window_hours=d.cross_platform_window_hours,
                length_prefilter_ratio=d.length_prefilter_ratio),
            "pruned": prune(self.conn, retention_days=self.hcfg.retention_days,
                            max_rows=self.hcfg.max_rows),
            "rows": self.conn.execute("SELECT COUNT(*) FROM hot_topics").fetchone()[0],
        }
        if report is not None:
            report["_storage"] = stats
        return stats

    # ------------------------------------------------------------------
    # 抓取
    # ------------------------------------------------------------------

    def _fetch_platform(self, platform: str, base_url: str, source_name: str) -> list[dict]:
        path = _PLATFORM_PATH.get(platform, platform)
        url = f"{base_url}/v2/{path}"
        body, _content_type = _fetch_public_text(
            url, timeout=self.hcfg.fetch_timeout_seconds,
            headers={"User-Agent": "iterwave/0.1 (+local)"},
        )
        payload = json.loads(body)
        if payload.get("code") not in (200, None):
            raise RuntimeError(f"接口返回 code={payload.get('code')} {payload.get('message', '')}")

        window_h = self.hcfg.filters.get("time_window_hours")
        now = datetime.now()
        out: list[dict] = []
        for raw in (payload.get("data") or [])[:self.hcfg.max_items_per_source]:
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

    def _fetch_custom(self, source: dict, label: str) -> list[dict]:
        """自定义行业源（RSS / JSON）→ 统一热点结构；无热度字段就不显示数字（R22）。"""
        url = str(source.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("行业源地址无效")
        body, content_type = _fetch_public_text(
            url, timeout=self.hcfg.fetch_timeout_seconds,
            headers={**{str(k): str(v) for k, v in (source.get("headers") or {}).items() if k},
                     "User-Agent": "iterwave/0.1 (+local)"},
        )
        feed_type = source.get("type") or "rss"
        if feed_type == "rss" or "xml" in content_type:
            from .kb_sources import parse_rss
            _, raw_rows = parse_rss(body)
            rows = [{"title": r.get("title"), "link": r.get("link"), "description": r.get("summary"),
                     "date": r.get("date"), "heat": None} for r in raw_rows]
        else:
            payload = json.loads(body)
            raw_rows = payload if isinstance(payload, list) else payload.get("items") or payload.get("data") or []
            rows = []
            for raw in raw_rows if isinstance(raw_rows, list) else []:
                if not isinstance(raw, dict):
                    continue
                rows.append({
                    "title": raw.get("title") or raw.get("name") or raw.get("headline"),
                    "link": raw.get("link") or raw.get("url"),
                    "description": raw.get("description") or raw.get("summary"),
                    "date": raw.get("date") or raw.get("time") or raw.get("published_at"),
                    "heat": raw.get("heat") or raw.get("hot") or raw.get("hot_value"),
                })
        window_h = self.hcfg.filters.get("time_window_hours")
        now = datetime.now()
        out: list[dict] = []
        for raw in rows[:self.hcfg.max_items_per_source]:
            title = (raw.get("title") or "").strip()
            if not title:
                continue
            heat = _parse_heat(raw.get("heat"), raw.get("description"))
            time_sensitive: Optional[int] = None
            event_at = _parse_feed_ts(raw.get("date"))
            if event_at is not None and window_h:
                time_sensitive = 1 if (now - event_at) <= timedelta(hours=float(window_h)) else 0
            out.append({
                "id": _topic_id(label, title),
                "title": title,
                "raw_url": raw.get("link"),
                "heat_score": heat,
                "heat_unit": "行业源" if heat is None else "热度",
                "time_sensitive": time_sensitive,
            })
        return out

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    def _upsert(self, source_key: str, source_label_or_rows, rows: Optional[list[dict]] = None) -> None:
        """去重规则 1：同一 id 只更新（热度 / 链接 / last_seen_at / seen_count），discovered_at 保持首次发现时间。

        兼容旧签名 ``_upsert(platform, rows)``。
        """
        from .hot_topic_dedup import backfill, normalize_title
        if rows is None:
            rows = list(source_label_or_rows or [])
            source_label = source_key
            source_key = f"{self.source_name}/{source_key}"
        else:
            source_label = str(source_label_or_rows)
        backfill(self.conn)   # 旧数据先补归一化字段并合并，保证新 id 不与旧行重复
        now = datetime.now().isoformat(timespec="seconds")
        platform = source_key.rsplit("/", 1)[-1]
        source = source_key
        seen_in_batch: set[str] = set()
        for r in rows:
            if r["id"] in seen_in_batch:
                continue        # 同一批次里归一化后重复的标题只算一次
            seen_in_batch.add(r["id"])
            self.conn.execute(
                "INSERT INTO hot_topics (id, source, raw_url, discovered_at, last_seen_at, evidence_status, "
                "title, heat_score, heat_unit, time_sensitive, dimension, platform, title_norm, seen_count, "
                "heat_prev, heat_peak) "
                "VALUES (?, ?, ?, ?, ?, 'collected', ?, ?, ?, ?, NULL, ?, ?, 1, NULL, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "source=excluded.source, raw_url=excluded.raw_url, "
                "last_seen_at=excluded.last_seen_at, evidence_status='collected', "
                "title=excluded.title, heat_prev=hot_topics.heat_score, "
                "heat_score=excluded.heat_score, heat_unit=excluded.heat_unit, "
                "heat_peak=MAX(COALESCE(hot_topics.heat_peak, hot_topics.heat_score), excluded.heat_score), "
                "time_sensitive=excluded.time_sensitive, seen_count=hot_topics.seen_count + 1",
                (
                    r["id"], source, r["raw_url"], now, now, r["title"],
                    r["heat_score"], r["heat_unit"], r["time_sensitive"],
                    platform, normalize_title(r["title"]), r["heat_score"],
                ),
            )
        # 该平台之前的失败占位行清理掉
        self.conn.execute(
            "DELETE FROM hot_topics WHERE source = ? AND evidence_status = 'failed'",
            (source,),
        )
        # 滚动：热榜是「本轮快照」，本轮没再出现的条目直接删除（不展示、不参与研判），
        # 避免停运几天后旧热点一直累积在库里。按 id 判定，避免同一秒内两次采集的边界问题。
        if seen_in_batch:
            placeholders = ",".join("?" * len(seen_in_batch))
            self.conn.execute(
                f"DELETE FROM hot_topics WHERE source = ? AND evidence_status = 'collected' "
                f"AND id NOT IN ({placeholders})",
                (source, *seen_in_batch),
            )
        # 主条目被删后，指向它的去重指针会悬挂
        self.conn.execute(
            "UPDATE hot_topics SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL "
            "AND duplicate_of NOT IN (SELECT id FROM hot_topics)"
        )
        self.conn.commit()

    def _record_failure(self, source_key: str, source_label: str, reason: str) -> None:
        """采集失败：落一行 failed 占位（heat_score = NULL，不显示数字）。"""
        source = source_key
        platform = source_label
        try:
            self.conn.execute(
                "INSERT INTO hot_topics (id, source, raw_url, discovered_at, last_seen_at, evidence_status, "
                "title, heat_score, heat_unit, time_sensitive, dimension) "
                "VALUES (?, ?, NULL, ?, ?, 'failed', ?, NULL, NULL, NULL, NULL) "
                "ON CONFLICT(id) DO UPDATE SET "
                "discovered_at=excluded.discovered_at, last_seen_at=excluded.last_seen_at, evidence_status='failed', "
                "heat_score=NULL, raw_url=NULL",
                (
                    f"hot-failed-{platform}", source,
                    datetime.now().isoformat(timespec="seconds"),
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
        return (datetime.now() - dt) < timedelta(minutes=self.refresh_interval_minutes())

    def refresh_interval_minutes(self) -> Optional[int]:
        """采集频率来自 scheduler.jobs.hot_topics_refresh；cron 触发时按 24h 判断是否过期。"""
        job = self.cfg.scheduler.job("hot_topics_refresh")
        return job.interval_minutes or 24 * 60

    def _last_collected_at(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(COALESCE(last_seen_at, discovered_at)) AS ts FROM hot_topics WHERE evidence_status != 'demo'"
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
        from .hot_topic_matcher import HotTopicMatcher, dimension_match_of, keyword_judgement
        matcher = HotTopicMatcher(self.cfg, self.conn, use_default_llm=True)
        current_version = matcher.version
        # 每个数据源只展示最近一批（_upsert 同批写同一个 discovered_at），旧批次的过期热点不再出现
        latest_batch: dict[str, str] = {}
        also_on: dict[str, list[dict]] = {}
        for row in rows:
            if row["evidence_status"] == "collected":
                src = row["source"] or ""
                seen = row["last_seen_at"] or row["discovered_at"] or ""
                if seen > latest_batch.get(src, ""):
                    latest_batch[src] = seen

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
                    "platform": redact(platform),
                    "source": redact(source),
                    "status": "failed",
                    "message": redact(row["title"] or ""),
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
            if row["evidence_status"] == "collected" and \
                    (row["last_seen_at"] or row["discovered_at"] or "") < latest_batch.get(source, ""):
                continue
            if row["duplicate_of"]:
                # 去重规则 2：跨平台重复只挂到主条目上展示，不单独出卡
                also_on.setdefault(row["duplicate_of"], []).append({
                    "platform": redact(platform), "title": row["title"], "url": row["raw_url"],
                    "heat": row["heat_score"], "heat_unit": row["heat_unit"],
                })
                continue

            sources.setdefault(platform, {
                "platform": redact(platform),
                "source": redact(source),
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
            kj = keyword_judgement(matcher.persona, title)
            dimension_match = dimension_match_of(row, current_version, threshold=self.cfg.hot_topic_matcher.match_threshold)
            fresh = row["match_version"] == current_version

            items.append({
                "id": row["id"],
                "title": title,
                "source": redact(source),
                "platform": redact(platform),
                "discovered_at": row["discovered_at"],
                "last_seen_at": row["last_seen_at"],
                # 采集时间（本轮在榜时间）：前端展示「这条热点是哪次采到的」
                "collected_at": row["last_seen_at"] or row["discovered_at"],
                "seen_count": row["seen_count"],
                "heat_prev": row["heat_prev"],
                "heat_peak": row["heat_peak"],
                "url": row["raw_url"],
                "heat": heat,
                "heat_unit": row["heat_unit"],
                "time_sensitive": row["time_sensitive"],
                "evidence_status": row["evidence_status"],
                "dimension": (row["match_dimension"] if fresh else None) or row["dimension"],
                # 🎯 维度匹配（强制门）：True 关联 / False 已研判无关或落入禁止方向 / None 待研判
                "dimension_match": dimension_match,
                "dimension_hits": kj["keyword_hits"],
                "match_score": row["match_score"] if fresh else None,
                "match_angle": row["match_angle"] if fresh else None,
                "match_reason": row["match_reason"] if fresh else None,
                "match_method": row["match_method"] if fresh else None,
                # ✅ 可发布性（强制门）：命中红线 / 落入禁止方向 → 不可用
                "publishable": not hit_redline and not (fresh and row["forbidden_hit"]),
                "redline_hits": hit_redline,
            })

        # 📈 趋势辅助判断：以「同数据源本轮热度中位数」为基准，且必须有上一轮数据才判趋势（R3）
        heats_by_platform: dict[str, list[float]] = {}
        for it in items:
            if it["heat"] is not None:
                heats_by_platform.setdefault(it["platform"], []).append(float(it["heat"]))
        medians = {p: statistics.median(v) for p, v in heats_by_platform.items() if v}
        for it in items:
            it["trend"], it["trend_reason"] = _trend_of(it, medians.get(it["platform"]), self.hcfg)

        for it in items:
            it["also_on"] = also_on.get(it["id"], [])
        return items, list(sources.values()) + list(failed.values())
