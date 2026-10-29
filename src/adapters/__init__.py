"""四平台适配器包."""

from .base import (
    Platform, PlatformAdapter,
    DraftResult, ContentToPublish, MetricsSnapshot,
    DraftStatus, FetchStatus, CircuitBreakerReason,
)
from .draft_saver import DraftSaver, PLATFORM_SELECTORS

# 各平台适配器延迟 import，避免循环依赖
__all__ = [
    "Platform", "PlatformAdapter", "DraftSaver", "PLATFORM_SELECTORS",
    "DraftResult", "ContentToPublish", "MetricsSnapshot",
    "DraftStatus", "FetchStatus", "CircuitBreakerReason",
]

# 各平台编辑器 URL（供 login_manager 扫码登录用）
PLATFORM_EDITOR_URLS: dict[Platform, str] = {
    Platform.XHS: "https://creator.xiaohongshu.com/publish/publish",
    Platform.DOUYIN: "https://creator.douyin.com/creator-micro/content/upload",
    Platform.KUAISHOU: "https://creator.kuaishou.com/content/upload",
    Platform.WECHAT_CHANNELS: "https://channels.weixin.qq.com/platform/post/create",
}


def get_adapters(cfg, db_conn=None) -> dict[Platform, "PlatformAdapter"]:
    """根据 config.platforms.enabled_keys() 构造启用的适配器字典。

    db_conn 可选：传入已有的连接避免并发锁冲突；不传则适配器内部 new。
    """
    from .xhs import XiaohongshuAdapter
    from .douyin import DouyinAdapter
    from .kuaishou import KuaishouAdapter
    from .wechat import WechatChannelsAdapter

    all_constructors = {
        Platform.XHS: XiaohongshuAdapter,
        Platform.DOUYIN: DouyinAdapter,
        Platform.KUAISHOU: KuaishouAdapter,
        Platform.WECHAT_CHANNELS: WechatChannelsAdapter,
    }

    enabled_keys = cfg.platforms.enabled_keys()
    result: dict[Platform, PlatformAdapter] = {}
    for platform, cls in all_constructors.items():
        if platform.value in enabled_keys:
            result[platform] = cls(cfg, db_conn)
    return result


def new_context_for_platform(platform: Platform):
    """真实创建带登录态的 BrowserContext（async generator）。

    用法（async）：
        async with new_context_for_platform(Platform.XHS) as ctx:
            page = await ctx.new_page()
            ...
    """
    from contextlib import asynccontextmanager
    from playwright.async_api import async_playwright
    from ..core.login_manager import background_headless, get_or_create_context

    @asynccontextmanager
    async def _ctx():
        async with async_playwright() as p:
            # 后台任务默认无头（不弹窗口）；ITERWAVE_BROWSER_HEADED=1 仅用于调试
            browser = await p.chromium.launch(headless=background_headless())
            ctx = await get_or_create_context(browser, platform)
            try:
                yield ctx
            finally:
                await ctx.close()
                await browser.close()

    return _ctx()
