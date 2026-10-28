"""抖音平台适配器."""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from .base import (
    PlatformAdapter, Platform, DraftResult, ContentToPublish,
    MetricsSnapshot, FetchStatus, AdapterError,
)
from .draft_saver import DraftSaver

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 运营选择器（G-02：Easel 无抖音评论脚本可复用，自写；R6.5 已查）
# 抖音改版时只在这里单点更新。全部为多候选防御式，找不到就显式抛错。
# ----------------------------------------------------------------------
_LOGIN_HINT = ".login-mask, [data-e2e='login-guide'], button:has-text('登录')"

_FETCH_COMMENTS_JS = """() => {
    const nodes = Array.from(document.querySelectorAll(
        "[data-e2e='comment-item'], [class*='comment-item'], .comment-mainContent"));
    const out = [];
    for (const n of nodes) {
        const authorEl = n.querySelector(
            "[data-e2e='comment-item-username'], [class*='user-name'], [class*='nickname']");
        const contentEl = n.querySelector(
            "[data-e2e='comment-item-content'], [class*='comment-content'], [class*='content']");
        const author = authorEl ? authorEl.textContent.trim() : '';
        const content = contentEl ? contentEl.textContent.trim() : '';
        if (!author && !content) continue;
        out.push({
            id: n.getAttribute('data-id') || n.getAttribute('data-comment-id') || '',
            author, content,
            user_id: n.getAttribute('data-user-id') || '',
        });
    }
    return out;
}"""

_FIND_REPLY_BTN_JS = """(nickname) => {
    const nodes = Array.from(document.querySelectorAll(
        "[data-e2e='comment-item'], [class*='comment-item']"));
    for (const n of nodes) {
        const authorEl = n.querySelector(
            "[data-e2e='comment-item-username'], [class*='user-name'], [class*='nickname']");
        if (!authorEl || authorEl.textContent.trim() !== nickname) continue;
        for (const el of n.querySelectorAll('*')) {
            if (el.textContent.trim() === '回复' && el.children.length === 0
                && el.offsetParent !== null) return el;
        }
    }
    return null;
}"""

_REPLY_INPUT_SELECTORS = [
    "[data-e2e='comment-input'] [contenteditable='true']",
    "[contenteditable='true']",
    "textarea",
]
_SEND_BTN_SELECTORS = [
    "[data-e2e='comment-send-btn']",
    "button:has-text('发送')",
    "[class*='send']",
]
_MAIN_INPUT_SELECTORS = [
    "[data-e2e='comment-input'] [contenteditable='true']",
    "[placeholder*='善语结善缘']",
    "[placeholder*='评论']",
    "[contenteditable='true']",
]


class DouyinAdapter(PlatformAdapter):
    """抖音创作者中心适配器."""

    platform = Platform.DOUYIN

    def __init__(self, cfg, db_conn):
        super().__init__(cfg, db_conn)
        self._saver = DraftSaver(Platform.DOUYIN)
        # 回复阶段需要昵称定位「回复」按钮，采集时缓存 {comment_id: meta}
        self._comment_cache: dict[str, dict] = {}

    @staticmethod
    def _video_url(draft_id: str) -> str:
        if draft_id.startswith("http"):
            return draft_id
        return f"https://www.douyin.com/video/{draft_id}"

    @staticmethod
    def _user_url(user_id: str) -> str:
        if user_id.startswith("http"):
            return user_id
        return f"https://www.douyin.com/user/{user_id}"

    async def save_draft(self, content: ContentToPublish) -> DraftResult:
        from . import new_context_for_platform
        async with new_context_for_platform(self.platform) as ctx:
            page = await ctx.new_page()
            try:
                return await self._saver.save_draft_via_page(page, content)
            finally:
                await page.close()

    async def fetch_metrics(
        self, platform_draft_id: Optional[str], title: str,
    ) -> MetricsSnapshot:
        """读回抖音作品数据（用 src/vendor/easel/ 的 read_douyin_works）。"""
        return await self._fetch_metrics_via_readback(platform_draft_id, title)

    async def test_login(self) -> bool:
        ok, _reason = await self._probe_creator_home(require_content=False)
        return ok

    async def test_lightweight_action(self) -> bool:
        ok, _reason = await self._probe_creator_home(require_content=True)
        return ok

    # ------------------------------------------------------------------
    # 运营动作（G-02，全部显式失败，不假成功）
    # ------------------------------------------------------------------

    async def fetch_new_comments(self, draft_id: str) -> list[dict]:
        """拉取某条抖音作品的评论（真实 DOM 采集）。

        无登录态 → NotLoggedInError；页面被踢到登录页 → NotLoggedInError。
        """
        self._require_login()
        ctx_mgr = await self._get_browser_context()
        async with ctx_mgr as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(self._video_url(draft_id), wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2_000)
                if await page.query_selector(_LOGIN_HINT):
                    raise AdapterError("抖音登录态已失效（页面出现登录提示）")
                for _ in range(3):  # 评论区懒加载
                    await page.mouse.wheel(0, 2_000)
                    await page.wait_for_timeout(700)

                raw = await page.evaluate(_FETCH_COMMENTS_JS) or []
                items: list[dict] = []
                for c in raw:
                    cid = str(c.get("id") or "").strip()
                    if not cid:
                        digest = hashlib.md5(
                            f"{c.get('author','')}|{c.get('content','')}".encode()
                        ).hexdigest()[:16]
                        cid = f"h:{digest}"
                    meta = {
                        "comment_id": cid,
                        "user_id": c.get("user_id") or "",
                        "content": c.get("content") or "",
                        "author": c.get("author") or "",
                    }
                    self._comment_cache[cid] = meta
                    items.append(meta)
                logger.info("[抖音] 采集到评论 %d 条（draft=%s）", len(items), draft_id)
                return items
            finally:
                await page.close()

    async def reply_comment(self, draft_id: str, comment_id: str, content: str) -> bool:
        self._require_login()
        meta = self._comment_cache.get(comment_id)
        if meta is None:
            raise AdapterError(
                f"comment_id={comment_id} 不在本次会话缓存中，需先 fetch_new_comments"
            )
        nickname = meta.get("author") or ""
        if not nickname:
            raise AdapterError(f"comment_id={comment_id} 缺少作者昵称，无法定位回复入口")

        ctx_mgr = await self._get_browser_context()
        async with ctx_mgr as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(self._video_url(draft_id), wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2_000)
                for _ in range(3):
                    await page.mouse.wheel(0, 2_000)
                    await page.wait_for_timeout(600)

                btn = await page.evaluate_handle(_FIND_REPLY_BTN_JS, nickname)
                element = btn.as_element()
                if element is None:
                    raise AdapterError(f"未找到 {nickname} 的「回复」按钮（抖音可能改版）")
                await element.click()
                await page.wait_for_timeout(800)

                filled = False
                for sel in _REPLY_INPUT_SELECTORS:
                    node = await page.query_selector(sel)
                    if node is None:
                        continue
                    await node.click()
                    await page.keyboard.type(content, delay=15)
                    filled = True
                    break
                if not filled:
                    raise AdapterError("未找到回复输入框（抖音可能改版）")

                sent = False
                for sel in _SEND_BTN_SELECTORS:
                    node = await page.query_selector(sel)
                    if node is None:
                        continue
                    await node.click()
                    sent = True
                    break
                if not sent:
                    raise AdapterError("未找到发送按钮（抖音可能改版）")

                await page.wait_for_timeout(1_800)
                remaining = await page.query_selector(
                    f"[contenteditable='true']:has-text('{content[:8]}')"
                )
                if remaining is not None:
                    logger.warning("[抖音] 回复内容仍留在输入框，可能未发送成功")
                    return False
                logger.info("[抖音] 已回复 comment=%s", comment_id)
                return True
            finally:
                await page.close()

    async def post_comment(self, target_url: str, content: str) -> Optional[str]:
        """主动评论。返回 None → 上层写 pending_verify（R17）。"""
        self._require_login()
        ctx_mgr = await self._get_browser_context()
        async with ctx_mgr as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2_000)

                input_node = None
                for sel in _MAIN_INPUT_SELECTORS:
                    input_node = await page.query_selector(sel)
                    if input_node is not None:
                        break
                if input_node is None:
                    raise AdapterError("未找到评论输入框（可能未登录或页面改版）")

                await input_node.click()
                await page.keyboard.type(content, delay=15)
                for sel in _SEND_BTN_SELECTORS:
                    node = await page.query_selector(sel)
                    if node is not None:
                        await node.click()
                        break
                else:
                    raise AdapterError("未找到发送按钮")

                await page.wait_for_timeout(1_800)
                logger.info("[抖音] 主动评论已提交 url=%s（无法确认评论 id）", target_url)
                return None  # R17
            finally:
                await page.close()

    async def follow_user(self, user_id: str) -> bool:
        self._require_login()
        ctx_mgr = await self._get_browser_context()
        async with ctx_mgr as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(self._user_url(user_id), wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2_000)

                if await page.query_selector("button:has-text('已关注')"):
                    return True  # 幂等：已关注不再点

                btn = await page.query_selector(
                    "[data-e2e='user-follow-btn'], button:has-text('关注')"
                )
                if btn is None:
                    raise AdapterError("未找到「关注」按钮（可能未登录或页面改版）")
                await btn.click()
                await page.wait_for_timeout(1_500)
                if await page.query_selector("button:has-text('已关注')") is None:
                    logger.warning("[抖音] 点击关注后按钮未变为「已关注」")
                    return False
                return True
            finally:
                await page.close()
