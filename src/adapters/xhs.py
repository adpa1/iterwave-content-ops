"""小红书平台适配器."""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from .base import (
    PlatformAdapter, Platform, DraftResult, ContentToPublish,
    MetricsSnapshot, FetchStatus, CircuitBreakerReason,
    AdapterError, NotLoggedInError,
)
from .draft_saver import DraftSaver

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 运营选择器（R4：复制自 Easel `skills/shared/scripts/xhs_comment.py`，Apache 2.0，
# 见 src/vendor/easel/NOTICE；小红书改版时只在这里单点更新）
# ----------------------------------------------------------------------
_LOGIN_HINT = ".login-container, [class*='login-btn'], button:has-text('登录')"

# 回复输入框（contenteditable div，多候选防御式）
_REPLY_INPUT_SELECTORS = [
    "[contenteditable='true']",
    "[placeholder*='回复']",
    "[placeholder*='说点']",
    "textarea",
]
# 发送按钮
_SEND_BTN_SELECTORS = [
    "button:has-text('发送')",
    "text=发送",
    "[class*='send-btn']",
    "[class*='submit']",
]
# 主评论输入框（发顶层评论）
_MAIN_INPUT_SELECTORS = [
    "[placeholder*='说点什么']",
    "[placeholder*='来聊聊']",
    "[placeholder*='说点']",
    ".comment-input-box [contenteditable='true']",
    ".note-comment [contenteditable='true']",
    "[class*='comment-input'] [contenteditable='true']",
    "[class*='comment-bar'] [contenteditable='true']",
    "[class*='commentInput'] [contenteditable='true']",
]

# 在某昵称的评论容器内找「回复」按钮（昵称作参数传入，避免选择器注入）
_FIND_REPLY_BTN_JS = """(nickname) => {
    const authors = Array.from(document.querySelectorAll('.author'));
    const target = authors.find(a => a.textContent.trim() === nickname);
    if (!target) return null;
    let container = target.parentElement;
    for (let i = 0; i < 6 && container; i++) {
        for (const el of container.querySelectorAll('*')) {
            if (el.textContent.trim() === '回复' && el.children.length === 0 && el.offsetParent !== null) {
                return el;
            }
        }
        container = container.parentElement;
    }
    return null;
}"""

# 抓评论列表：作者 + 内容 + 尽量取稳定 id
_FETCH_COMMENTS_JS = """() => {
    const nodes = Array.from(document.querySelectorAll(
        "[class*='comment-item'], .comment-item, [class*='commentItem']"));
    const out = [];
    for (const n of nodes) {
        const authorEl = n.querySelector('.author, [class*='author'], [class*='name']');
        const contentEl = n.querySelector('.content, [class*='content'], [class*='note-text']');
        const author = authorEl ? authorEl.textContent.trim() : '';
        const content = contentEl ? contentEl.textContent.trim() : '';
        if (!author && !content) continue;
        const idAttr = n.getAttribute('data-id') || n.getAttribute('data-comment-id')
            || (n.id && n.id.startsWith('comment') ? n.id : '');
        out.push({ id: idAttr || '', author, content, user_id: n.getAttribute('data-user-id') || '' });
    }
    return out;
}"""


class XiaohongshuAdapter(PlatformAdapter):
    """小红书创作者中心适配器。"""

    platform = Platform.XHS

    def __init__(self, cfg, db_conn):
        super().__init__(cfg, db_conn)
        self._saver = DraftSaver(Platform.XHS)
        # G-02：评论回复需要昵称定位「回复」按钮，采集时缓存 {comment_id: meta}
        self._comment_cache: dict[str, dict] = {}
        # R4：复用的是 src/vendor/easel/ 里的复制件，不依赖 Easel 源码目录

    @staticmethod
    def _note_url(draft_id: str) -> str:
        """draft_id 可能是完整 URL，也可能是纯 note_id。"""
        if draft_id.startswith("http"):
            return draft_id
        return f"https://www.xiaohongshu.com/explore/{draft_id}"

    # ------------------------------------------------------------------
    # 存草稿
    # ------------------------------------------------------------------

    async def save_draft(self, content: ContentToPublish) -> DraftResult:
        """用 DraftSaver 在小红书创作者中心存草稿（只点存草稿按钮，R1 红线）。"""
        from . import new_context_for_platform
        async with new_context_for_platform(self.platform) as ctx:
            page = await ctx.new_page()
            try:
                return await self._saver.save_draft_via_page(page, content)
            finally:
                await page.close()

    # ------------------------------------------------------------------
    # 数据采集
    # ------------------------------------------------------------------

    async def fetch_metrics(
        self, platform_draft_id: Optional[str], title: str,
    ) -> MetricsSnapshot:
        """从小红书创作者中心读回数据。

        复用 src/vendor/easel/account_stats._scrape_notes 拿笔记卡片，再解析计数；
        解析不到的字段保持 None（R3）。
        """
        return await self._fetch_metrics_via_readback(platform_draft_id, title)

    # ------------------------------------------------------------------
    # 登录态
    # ------------------------------------------------------------------

    async def test_login(self) -> bool:
        """R6 熔断恢复第二步：真实探测创作者中心登录态（只读）。"""
        ok, _reason = await self._probe_creator_home(require_content=False)
        return ok

    async def test_lightweight_action(self) -> bool:
        """R6 熔断恢复第三步：真实读取创作者中心首页（只读，无写操作）。"""
        ok, _reason = await self._probe_creator_home(require_content=True)
        return ok

    # ------------------------------------------------------------------
    # 运营动作（全部幂等，R16）
    # ------------------------------------------------------------------

    async def fetch_new_comments(self, draft_id: str) -> list[dict]:
        """拉取某篇已发布笔记的评论列表（真实 DOM 采集）。

        G-02：无登录态 → NotLoggedInError（ops_logs 记 failed），不返回假空列表。
        """
        self._require_login()
        ctx_mgr = await self._get_browser_context()
        async with ctx_mgr as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(self._note_url(draft_id), wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1_500)
                if await page.query_selector(_LOGIN_HINT):
                    raise NotLoggedInError("小红书登录态已失效（页面出现登录提示）")
                for _ in range(3):  # 评论区懒加载：滚到底再抓
                    await page.mouse.wheel(0, 2_000)
                    await page.wait_for_timeout(600)

                raw = await page.evaluate(_FETCH_COMMENTS_JS) or []
                items: list[dict] = []
                for c in raw:
                    cid = str(c.get("id") or "").strip()
                    if not cid:
                        # 平台没给稳定 id 时，用 author+content 的 hash 作为幂等键（同一评论重扫仍同键）
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
                    # 回复阶段需要昵称定位按钮：缓存到本适配器实例（进程内）
                    self._comment_cache[cid] = meta
                    items.append(meta)
                logger.info("[小红书] 采集到评论 %d 条（draft=%s）", len(items), draft_id)
                return items
            finally:
                await page.close()

    async def reply_comment(self, draft_id: str, comment_id: str, content: str) -> bool:
        """被动回复评论（真实点击「回复」→ 输入 → 发送 → 校验）。"""
        self._require_login()
        meta = self._comment_cache.get(comment_id)
        if meta is None:
            raise AdapterError(
                f"comment_id={comment_id} 不在本次会话缓存中，无法定位评论作者；"
                f"需先 fetch_new_comments（R16 幂等流程）"
            )
        nickname = meta.get("author") or ""
        if not nickname:
            raise AdapterError(f"comment_id={comment_id} 缺少作者昵称，无法定位回复入口")

        ctx_mgr = await self._get_browser_context()
        async with ctx_mgr as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(self._note_url(draft_id), wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1_500)
                for _ in range(3):
                    await page.mouse.wheel(0, 2_000)
                    await page.wait_for_timeout(500)

                btn = await page.evaluate_handle(_FIND_REPLY_BTN_JS, nickname)
                element = btn.as_element()
                if element is None:
                    raise AdapterError(f"未找到 {nickname} 的「回复」按钮（小红书可能改版）")
                await element.click()
                await page.wait_for_timeout(600)

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
                    raise AdapterError("未找到回复输入框（小红书可能改版）")

                sent = False
                for sel in _SEND_BTN_SELECTORS:
                    node = await page.query_selector(sel)
                    if node is None:
                        continue
                    await node.click()
                    sent = True
                    break
                if not sent:
                    raise AdapterError("未找到发送按钮（小红书可能改版）")

                await page.wait_for_timeout(1_800)
                remaining = await page.query_selector(
                    f"[contenteditable='true']:has-text('{content[:8]}')"
                )
                if remaining is not None:
                    logger.warning("[小红书] 回复内容仍留在输入框，可能未发送成功")
                    return False
                logger.info("[小红书] 已回复 comment=%s", comment_id)
                return True
            finally:
                await page.close()

    async def post_comment(self, target_url: str, content: str) -> Optional[str]:
        """主动评论。返回 None = 无法确认是否发出 → 上层标 pending_verify（R17）。"""
        self._require_login()
        ctx_mgr = await self._get_browser_context()
        async with ctx_mgr as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1_500)

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
                logger.info("[小红书] 主动评论已提交 url=%s（无法确认评论 id）", target_url)
                return None  # R17：返回 None 让上层写入 pending_verify
            finally:
                await page.close()

    async def follow_user(self, user_id: str) -> bool:
        """关注用户（真实点击，校验按钮文案变为「已关注」）。"""
        self._require_login()
        url = user_id if user_id.startswith("http") else f"https://www.xiaohongshu.com/user/profile/{user_id}"
        ctx_mgr = await self._get_browser_context()
        async with ctx_mgr as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1_500)

                followed = await page.query_selector("button:has-text('已关注')")
                if followed is not None:
                    return True  # 幂等：已关注不再点

                btn = await page.query_selector("button:has-text('关注')")
                if btn is None:
                    raise AdapterError("未找到「关注」按钮（可能未登录或页面改版）")
                await btn.click()
                await page.wait_for_timeout(1_500)
                if await page.query_selector("button:has-text('已关注')") is None:
                    logger.warning("[小红书] 点击关注后按钮未变为「已关注」")
                    return False
                return True
            finally:
                await page.close()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

