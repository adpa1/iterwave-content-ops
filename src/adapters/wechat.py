"""视频号平台适配器.

能力边界（G-02）：视频号的评论/关注互动只在微信客户端内发生，
网页端 channels.weixin.qq.com 只有发布与数据看板，没有评论区与关注按钮。
因此这四个运营动作一律抛 PlatformUnsupportedError，由 ops/engine 记 skipped
（不是 failed），避免每天定时任务刷失败告警。
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import (
    PlatformAdapter, Platform, DraftResult, ContentToPublish,
    MetricsSnapshot, FetchStatus, PlatformUnsupportedError,
)
from .draft_saver import DraftSaver

logger = logging.getLogger(__name__)

_UNSUPPORTED = (
    "视频号网页端（channels.weixin.qq.com）没有评论区/关注入口，"
    "评论互动只能在微信客户端内手动完成（平台能力限制，非未实现）"
)


class WechatChannelsAdapter(PlatformAdapter):
    """视频号创作者中心适配器."""

    platform = Platform.WECHAT_CHANNELS

    def __init__(self, cfg, db_conn):
        super().__init__(cfg, db_conn)
        self._saver = DraftSaver(Platform.WECHAT_CHANNELS)

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
        """读回视频号作品数据（Easel 无逐篇读回，走保守文本解析）。"""
        return await self._fetch_metrics_via_readback(platform_draft_id, title)

    async def test_login(self) -> bool:
        ok, _reason = await self._probe_creator_home(require_content=False)
        return ok

    async def test_lightweight_action(self) -> bool:
        ok, _reason = await self._probe_creator_home(require_content=True)
        return ok

    # ------------------------------------------------------------------
    # 运营动作：平台能力不支持（不假成功、不刷 failed）
    # ------------------------------------------------------------------

    async def fetch_new_comments(self, draft_id: str) -> list[dict]:
        self._require_login()
        raise PlatformUnsupportedError(_UNSUPPORTED)

    async def reply_comment(self, draft_id: str, comment_id: str, content: str) -> bool:
        self._require_login()
        raise PlatformUnsupportedError(_UNSUPPORTED)

    async def post_comment(self, target_url: str, content: str) -> Optional[str]:
        self._require_login()
        raise PlatformUnsupportedError(_UNSUPPORTED)

    async def follow_user(self, user_id: str) -> bool:
        self._require_login()
        raise PlatformUnsupportedError(_UNSUPPORTED)
