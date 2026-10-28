"""
数据读回 —— 用本仓库复制的 Easel 代码（src/vendor/easel，R4：复制而非外部调用）。

分工：
- 抖音 / 快手 → 调用 vendor 的 `read_douyin_works` / `read_kuaishou_works`
  （页面内 fetch 接口，字段最可靠）
- 小红书 → 复用 vendor 的 `account_stats._scrape_notes` 拿卡片文本，再解析计数
  （上游的 xhs 读回不返回逐篇数字，这里补一层文本解析）
- 视频号 → 上游无逐篇读回，走保守文本解析；解析不到就如实报 parse_error

R3 红线：解析不到的字段一律 None，**绝不补 0**；一个数字都没解析到 → parse_error。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from .base import Platform, FetchStatus, MetricsSnapshot
from ..vendor.easel import VENDOR_DIR, ensure_vendor_path

logger = logging.getLogger(__name__)

# 复制代码所在目录（本仓库内，与 Easel 项目无运行时依赖）
DEFAULT_EASEL_SCRIPTS = VENDOR_DIR

_EASEL_READY = False
_EASEL_ERROR: Optional[str] = None


def _ensure_easel_importable() -> Optional[str]:
    """把 vendor 目录加进 sys.path。返回错误信息或 None。"""
    global _EASEL_READY, _EASEL_ERROR
    if _EASEL_READY:
        return None
    if _EASEL_ERROR:
        return _EASEL_ERROR

    if not DEFAULT_EASEL_SCRIPTS.is_dir():
        _EASEL_ERROR = f"vendor 脚本目录不存在: {DEFAULT_EASEL_SCRIPTS}"
        return _EASEL_ERROR
    ensure_vendor_path()
    _EASEL_READY = True
    return None


# =====================================================================
# 数字解析（"1.2万" / "3,456" / "12" → int，解析不到 → None）
# =====================================================================

_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(万|w|W|亿)?")


def parse_cn_number(raw: Any) -> Optional[int]:
    """把中文计数转 int。无法解析返回 None（R3：不返回 0 冒充）。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (ValueError, OverflowError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    m = _NUM_RE.search(text.replace("+", ""))
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2)
    if unit in ("万", "w", "W"):
        value *= 10000
    elif unit == "亿":
        value *= 100000000
    return int(value)


# =====================================================================
# 小红书 / 视频号：从卡片文本里抽计数
# =====================================================================

# 中文平台的计数标签（顺序有讲究：先匹配更长的词）
_LABEL_PATTERNS: dict[str, tuple[str, ...]] = {
    "likes": ("点赞", "赞", "digg"),
    "collects": ("收藏", "collect"),
    "comments": ("评论", "comment"),
    "shares": ("分享", "转发", "share"),
    "views": ("播放", "观看", "浏览", "阅读", "view"),
}


def _extract_counts_from_text(text: str) -> dict[str, Optional[int]]:
    """从卡片纯文本里抽点赞/收藏/评论/分享/播放。抽不到 = None。"""
    result: dict[str, Optional[int]] = {k: None for k in _LABEL_PATTERNS}
    if not text:
        return result

    for field, labels in _LABEL_PATTERNS.items():
        for label in labels:
            # "点赞 1.2万" / "1.2万 点赞" 两种顺序都试
            m = re.search(rf"{label}\s*[:：]?\s*(\d+(?:[.,]\d+)?\s*(?:万|亿)?)", text)
            if not m:
                m = re.search(rf"(\d+(?:[.,]\d+)?\s*(?:万|亿)?)\s*{label}", text)
            if m:
                result[field] = parse_cn_number(m.group(1))
                if result[field] is not None:
                    break
    return result


# =====================================================================
# 统一读回
# =====================================================================

class ReadbackUnavailable(RuntimeError):
    """该平台没有可用读回实现。"""


def read_works(page, platform: Platform, limit: int = 20) -> list[dict]:
    """读取本人作品列表，返回归一化 dict 列表。

    {
      "content_id": str, "title": str, "published_at_ms": int|None,
      "stats": {"views","likes","collects","comments","shares"}  # 值可能 None
    }
    失败抛异常，由调用方转成 fetch_status。
    """
    err = _ensure_easel_importable()
    if err:
        raise ReadbackUnavailable(err)

    if platform == Platform.DOUYIN:
        import platform_readback as pr

        works = pr.read_douyin_works(page, limit=limit)
        return [
            {
                "content_id": w.platform_content_id,
                "title": w.title or "",
                "published_at_ms": w.published_at_ms,
                "stats": {
                    "views": _as_int(w.stats.get("play_count")),
                    "likes": _as_int(w.stats.get("digg_count")),
                    "collects": None,
                    "comments": _as_int(w.stats.get("comment_count")),
                    "shares": None,
                },
            }
            for w in works
        ]

    if platform == Platform.KUAISHOU:
        import platform_readback as pr

        works = pr.read_kuaishou_works(page, limit=limit)
        return [
            {
                "content_id": w.platform_content_id,
                "title": w.title or "",
                "published_at_ms": w.published_at_ms,
                "stats": {
                    "views": _as_int(w.stats.get("play_count")),
                    "likes": _as_int(w.stats.get("like_count")),
                    "collects": None,
                    "comments": _as_int(w.stats.get("comment_count")),
                    "shares": None,
                },
            }
            for w in works
        ]

    if platform == Platform.XHS:
        import account_stats as acc

        notes = acc._scrape_notes("xiaohongshu", page, {})  # noqa: SLF001 — 复用 vendor 复制件的解析
        out: list[dict] = []
        for note in notes or []:
            counts = _extract_counts_from_text(note.get("text") or "")
            out.append({
                "content_id": note.get("note_id") or "",
                "title": note.get("title") or "",
                "published_at_ms": None,
                "stats": counts,
            })
        return out

    if platform == Platform.WECHAT_CHANNELS:
        # Easel 没有视频号逐篇读回：用页面文本兜底，解析不到就如实失败
        page.goto("https://channels.weixin.qq.com/platform/post/list",
                  wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        if not text.strip():
            return []
        # 无稳定结构，只能整体抽一次（对不上具体篇目 → 由调用方判 parse_error）
        return [{
            "content_id": "",
            "title": "",
            "published_at_ms": None,
            "stats": _extract_counts_from_text(text),
        }]

    raise ReadbackUnavailable(f"不支持的平台: {platform}")


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def match_work(works: list[dict], title: str, platform_draft_id: Optional[str] = None) -> Optional[dict]:
    """按平台草稿 ID 优先、标题前缀兜底匹配。"""
    if platform_draft_id:
        for w in works:
            if w.get("content_id") and w["content_id"] == platform_draft_id:
                return w
    key = (title or "").strip().replace("\n", " ")[:20]
    if not key:
        return None
    for w in works:
        if key in (w.get("title") or ""):
            return w
    return None


def snapshot_from_work(platform: Platform, work: dict, raw: str) -> MetricsSnapshot:
    """把归一化作品字典转成 MetricsSnapshot，并按字段完整度定 fetch_status。

    R3：字段缺失保持 None；一个数字都没有 → parse_error（不是 success + 0）。
    """
    stats = work.get("stats") or {}
    views = stats.get("views")
    likes = stats.get("likes")
    collects = stats.get("collects")
    comments = stats.get("comments")
    shares = stats.get("shares")

    # 各平台核心指标维度不同：抖音/快手无 collects(收藏)/shares(分享) 概念，
    # 以 views/likes/comments 为核心维度；小红书/视频号需 5 项齐全（D5）。
    core_dims = (
        ("views", "likes", "comments")
        if platform in (Platform.DOUYIN, Platform.KUAISHOU)
        else ("views", "likes", "collects", "comments", "shares")
    )
    dims = (
        ("views", views), ("likes", likes), ("collects", collects),
        ("comments", comments), ("shares", shares),
    )
    present = [v for _, v in dims if v is not None]
    core_present = [v for k, v in dims if k in core_dims and v is not None]

    if not present:
        status = FetchStatus.PARSE_ERROR
        error = "页面结构变化或未登录，未解析到任何指标"
    elif len(core_present) < len(core_dims):
        missing = [k for k, v in dims if k in core_dims and v is None]
        status = FetchStatus.PARTIAL
        error = f"仅解析到 {len(core_present)}/{len(core_dims)} 项核心指标（缺失：{','.join(missing)}）"
    else:
        status = FetchStatus.SUCCESS
        error = None

    return MetricsSnapshot(
        platform=platform,
        fetch_status=status,
        likes=likes,
        collects=collects,
        comments=comments,
        shares=shares,
        views=views,
        raw_response=raw,
        error_msg=error,
    )


def failed_snapshot(platform: Platform, reason: str) -> MetricsSnapshot:
    return MetricsSnapshot(
        platform=platform,
        fetch_status=FetchStatus.FAILED,
        error_msg=reason,
    )
