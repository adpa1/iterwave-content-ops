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


# 小红书 note-manager 卡片：日期之后是 5 个裸数字（没有「点赞/收藏」标签），
# 实测顺序 = 曝光（观看）/ 点赞 / 收藏 / 评论 / 分享。
_XHS_TAIL_NUM_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?[^\d]*"
    r"((?:\d+(?:[.,]\d+)?(?:万|亿)?\s+){4}\d+(?:[.,]\d+)?(?:万|亿)?)\s*$"
)


def _parse_xhs_card_counts(text: str) -> dict[str, Optional[int]]:
    """按「日期后 5 个裸数字」解析小红书卡片；不是 5 个 → 全 None（R3）。"""
    result: dict[str, Optional[int]] = {k: None for k in _LABEL_PATTERNS}
    m = _XHS_TAIL_NUM_RE.search(text or "")
    if not m:
        return result
    parts = m.group(1).split()
    if len(parts) != 5:
        return result
    for key, raw in zip(("views", "likes", "collects", "comments", "shares"), parts):
        result[key] = parse_cn_number(raw)
    return result


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


def read_works_sync(platform: Platform, limit: int = 20) -> list[dict]:
    """在独立线程里用**同步** Playwright 跑 vendor 读回（vendor 是同步实现）。

    base._fetch_metrics_via_readback 跑在 async Playwright 上；把 async Page 传给
    同步 vendor 只会拿到未 await 的 coroutine（实测 fetch_metrics 永远 failed，
    伴随 "coroutine 'Page.wait_for_selector' was never awaited"）。因此这里自己起
    一个 headed 浏览器 + storage_state，调用方用 asyncio.to_thread 包一层。
    """
    err = _ensure_easel_importable()
    if err:
        raise ReadbackUnavailable(err)

    from playwright.sync_api import sync_playwright
    from ..core.login_manager import (
        _platform_state_path, background_headless, has_login_state,
    )

    if not has_login_state(platform):
        raise ReadbackUnavailable(f"{platform.display_name} 无登录态文件，请先扫码登录")

    with sync_playwright() as p:
        # 定时采集默认无头（不弹窗口）；ITERWAVE_BROWSER_HEADED=1 仅用于调试
        browser = p.chromium.launch(headless=background_headless())
        try:
            ctx = browser.new_context(storage_state=str(_platform_state_path(platform)))
            page = ctx.new_page()
            try:
                return read_works(page, platform, limit=limit)
            finally:
                page.close()
                ctx.close()
        finally:
            browser.close()


def read_works(page, platform: Platform, limit: int = 20) -> list[dict]:
    """读取本人作品列表，返回归一化 dict 列表。

    {
      "content_id": str, "title": str, "url": str, "published_at_ms": int|None,
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

        # 不能直接用 acc._scrape_notes：它返回的 dict 只有 title/note_id/url/cover/stat（stat 为空），
        # 丢掉了卡片纯文本，导致计数永远是 None → 每篇都 parse_error。这里复用它的导航/判定
        # 与 _XHS_NOTES_JS，但自己保留 text 和可点链接。
        # note-manager 的卡片不是 <a>，href 为空；xsec_token 要从它的 JSON 接口里截获，
        # 才能拼出可直接打开（并抓评论）的笔记链接。
        token_map: dict[str, str] = {}

        def _on_resp(resp) -> None:
            try:
                if "json" in ((resp.headers or {}).get("content-type", "")):
                    token_map.update(acc._extract_note_tokens(resp.json()))
            except Exception:  # noqa: BLE001 — 单条响应解析失败不影响整体
                pass

        page.on("response", _on_resp)
        try:
            page.goto("https://creator.xiaohongshu.com/new/note-manager",
                      wait_until="commit", timeout=15000)
            if acc._login_wall(page):  # noqa: SLF001 — 复用 vendor 纯函数
                raise ReadbackUnavailable("小红书登录态失效（note-manager 出现登录墙）")
            try:
                page.wait_for_selector(".note-card, [role='tab']", timeout=10000)
            except Exception as exc:  # noqa: BLE001
                raise ReadbackUnavailable(f"小红书 note-manager 未加载出笔记卡片: {exc}") from exc
            acc._xhs_open_published_notes(page)  # noqa: SLF001
            try:
                page.wait_for_selector(".note-card", timeout=8000)
            except Exception:  # noqa: BLE001
                return []
            page.wait_for_timeout(800)
            raw_notes = page.evaluate(acc._XHS_NOTES_JS) or []  # noqa: SLF001
        finally:
            try:
                page.remove_listener("response", _on_resp)
            except Exception:  # noqa: BLE001
                pass

        out: list[dict] = []
        for note in raw_notes:
            if not acc.is_public_xhs_note(note):  # noqa: SLF001
                continue
            nid = note.get("noteId") or ""
            tok = token_map.get(nid, "")
            url = (
                f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={tok}&xsec_source=pc_creatormng"
                if nid and tok else
                f"https://www.xiaohongshu.com/explore/{nid}" if nid else ""
            )
            text = note.get("text") or ""
            counts = _parse_xhs_card_counts(text)
            if all(v is None for v in counts.values()):
                counts = _extract_counts_from_text(text)  # 有标签的改版兜底
            out.append({
                "content_id": nid,
                "title": note.get("title") or "",
                "url": url,
                "published_at_ms": None,
                "stats": counts,
            })
        return out[:limit]

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
