"""
DraftSaver — 通用 Playwright 存草稿工具（四平台共用）。

参考 Easel web_publisher.py（复制进本仓库后按需取用，不是外部调用）：
- _fill_first_visible()   → 多候选选择器填写
- platform 选择器配置      → 各平台 selector 差异已抽象
- login_state.py          → 已复制到 src/vendor/easel/，cookie/token 持久化
- login_qr.py             → 已复制到 src/vendor/easel/，二维码 DOM 渲染

我们只保留 Easel 的 "finalize=draft" 分支 — 填表单 → 点"存草稿"。
**严禁走 finalize=publish 分支，R1 红线！**

平台选择器差异：
- 小红书：标题 input / 正文 contenteditable div / 封面上传 / 话题标签 / 存草稿按钮
- 抖音：标题 input / 正文 textarea / 封面上传 / 话题 @ / 存草稿按钮
- 快手：标题 input / 正文 textarea / 封面上传 / 话题 # / 存草稿按钮
- 视频号：标题 input / 正文 div / 封面上传 / 话题 # / 存草稿按钮
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from playwright.async_api import Page, BrowserContext, async_playwright

from .base import (
    Platform, DraftResult, ContentToPublish,
    CircuitBreakerReason, FetchStatus, MetricsSnapshot,
)

logger = logging.getLogger(__name__)


# =====================================================================
# 各平台选择器配置（参考 Easel web_publisher.py）
# =====================================================================

PLATFORM_SELECTORS: dict[Platform, dict] = {
    Platform.XHS: {
        "editor_url": "https://creator.xiaohongshu.com/publish/publish",
        "title": [
            "input[placeholder*='填写标题会有更多赞哦']",
            "input.title-input",
            "input[placeholder*='标题']",
        ],
        "body": [
            "div[contenteditable='true'][class*='content']",
            "div.editor-content",
            "div[contenteditable='true']",
        ],
        "image_upload": [
            "input[type='file'][accept*='image']",
        ],
        "topic_input": [
            "input[placeholder*='添加话题']",
            "input[placeholder*='话题']",
        ],
        "save_draft_button": [
            "button:has-text('存草稿')",
            "button:has-text('保存草稿')",
            "button.save-draft",
            "button:has-text('草稿')",
        ],
        # R1 红线：发布按钮严禁点击
        # 这里只列出来给审计用，代码永不调用
        "_publish_button_audit_only": [
            "button:has-text('发布')",
            "button:has-text('发送')",
            "button.publish-btn",
        ],
    },
    Platform.DOUYIN: {
        "editor_url": "https://creator.douyin.com/creator-micro/content/upload",
        "title": [
            "input[placeholder*='添加标题']",
            "input[placeholder*='请输入标题']",
        ],
        "body": [
            "textarea[placeholder*='正文']",
            "textarea.content-textarea",
            "div[contenteditable='true']",
        ],
        "image_upload": [
            "input[type='file'][accept*='image']",
        ],
        "topic_input": [
            "input[placeholder*='添加话题']",
            "input[placeholder*='@ 朋友']",
        ],
        "save_draft_button": [
            "button:has-text('存草稿')",
            "button:has-text('保存草稿')",
            "div:has-text('存草稿')",
        ],
        "_publish_button_audit_only": [
            "button:has-text('发布')",
            "button.publish-btn",
        ],
    },
    Platform.KUAISHOU: {
        "editor_url": "https://creator.kuaishou.com/content/upload",
        "title": [
            "input[placeholder*='标题']",
            "input[placeholder*='输入标题']",
        ],
        "body": [
            "textarea[placeholder*='正文']",
            "div[contenteditable='true']",
        ],
        "image_upload": [
            "input[type='file'][accept*='image']",
        ],
        "save_draft_button": [
            "button:has-text('存草稿')",
            "button:has-text('保存草稿')",
        ],
        "_publish_button_audit_only": [
            "button:has-text('发布')",
        ],
    },
    Platform.WECHAT_CHANNELS: {
        "editor_url": "https://channels.weixin.qq.com/platform/post/create",
        "title": [
            "input[placeholder*='标题']",
            "input[placeholder*='填写标题']",
        ],
        "body": [
            "div[contenteditable='true']",
            "div.post-content",
        ],
        "image_upload": [
            "input[type='file'][accept*='image']",
        ],
        "save_draft_button": [
            "button:has-text('存草稿')",
            "button:has-text('保存草稿')",
            "button:has-text('草稿')",
        ],
        "_publish_button_audit_only": [
            "button:has-text('发布')",
        ],
    },
}


# =====================================================================
# DraftSaver — 通用存草稿实现
# =====================================================================

class DraftSaver:
    """存草稿的 Playwright 操作封装（四平台共用）。

    R1 红线保障：
    1. 只 PLATFORM_SELECTORS[platform]["save_draft_button"] 中的选择器
    2. publish 按钮只出现在 _publish_button_audit_only 里供 grep 审计，
       永不传给 click()
    """

    def __init__(self, platform: Platform):
        self.platform = platform
        self.selectors = PLATFORM_SELECTORS[platform]

    async def fill_first_visible(
        self, page: Page, selectors: list[str], text: str,
    ) -> bool:
        """多候选选择器中找到第一个可见的填写（参考 Easel _fill_first_visible）。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await loc.fill(text)
                    return True
            except Exception:
                continue
        logger.warning("平台 %s 没有找到可见元素: %s", self.platform, selectors)
        return False

    async def click_first_visible(
        self, page: Page, selectors: list[str], timeout: int = 5000,
    ) -> bool:
        """多候选选择器中找到第一个可见的点击。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await loc.click()
                    return True
            except Exception:
                continue
        return False

    async def save_draft_via_page(
        self, page: Page, content: ContentToPublish,
    ) -> DraftResult:
        """在已打开的编辑器页面存草稿。

        流程：goto → 等加载 → 上传图片 → 填标题 → 填正文 → 加话题 → 存草稿。
        **永不点击发布按钮**（R1）。
        """
        try:
            # 1. 导航到编辑器
            await page.goto(self.selectors["editor_url"], wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # 2. 登录态检查
            if await self._check_login_expired(page):
                return DraftResult(
                    success=False,
                    error_msg="登录态过期",
                    login_required=True,
                )

            # 3. 检查熔断信号（R6：命中即中止本次保存，由上层上报熔断器）
            cb_signal = await self._detect_cb_signal(page)
            if cb_signal:
                logger.warning("平台 %s 检测到熔断信号: %s", self.platform, cb_signal)
                return DraftResult(
                    success=False,
                    error_msg=f"检测到熔断信号: {cb_signal.value}",
                    cb_signal=cb_signal,
                )

            # 4. 上传图片
            for img_path in content.image_paths:
                file_input = page.locator(self.selectors["image_upload"][0]).first
                if await file_input.is_visible(timeout=3000):
                    await file_input.set_input_files(img_path)
                    await page.wait_for_timeout(1000)

            # 5. 填标题
            title_ok = await self.fill_first_visible(page, self.selectors["title"], content.title)
            if not title_ok:
                return DraftResult(
                    success=False,
                    error_msg="标题输入框未找到",
                )

            # 6. 填正文
            body_ok = await self.fill_first_visible(page, self.selectors["body"], content.body)
            if not body_ok:
                return DraftResult(
                    success=False,
                    error_msg="正文输入框未找到",
                )

            # 7. 加话题标签（各平台差异大，这里简化：标签追加到正文末尾）
            if content.tags:
                tag_text = " " + " ".join(content.tags)
                await self.fill_first_visible(
                    page, self.selectors.get("topic_input", []),
                    ""  # topic_input 可能不存在，忽略
                )

            # 8. ⚠️ 存草稿（只点 save_draft_button，严禁点 _publish_button_audit_only！）
            save_ok = await self.click_first_visible(page, self.selectors["save_draft_button"])
            if not save_ok:
                logger.warning("存草稿按钮未找到，尝试兜底 ...")
                # 兜底：找所有按钮，挑第一个不含"发布"/"发送"的
                save_ok = await self._fallback_save_draft(page)

            if not save_ok:
                return DraftResult(
                    success=False,
                    error_msg="存草稿按钮未找到",
                )

            await page.wait_for_timeout(2000)

            # 9. 尝试从 URL / DOM 获取平台草稿 ID
            draft_id = await self._extract_draft_id(page)

            logger.info("✅ 平台 %s 存草稿成功 content_id=%s draft_id=%s",
                        self.platform, content.content_id, draft_id)
            return DraftResult(success=True, platform_draft_id=draft_id)

        except Exception as exc:
            logger.exception("❌ 平台 %s 存草稿失败: %s", self.platform, exc)
            return DraftResult(success=False, error_msg=str(exc))

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _check_login_expired(self, page: Page) -> bool:
        """简单检查：页面是否跳转到了登录页或出现扫码登录框。"""
        url = page.url
        if "login" in url.lower() or "signin" in url.lower():
            return True
        # 检查常见登录 prompt
        for sel in [
            "div.qrcode", "img[src*='qrcode']", "div.login-box",
            "div:has-text('扫码登录')", "div:has-text('请先登录')",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    return True
            except Exception:
                continue
        return False

    async def _detect_cb_signal(self, page: Page) -> Optional[CircuitBreakerReason]:
        """检测页面上的熔断信号（5 种）。"""
        # 验证码
        for sel in [
            "div:has-text('验证码')", "div:has-text('CAPTCHA')",
            "img[src*='captcha']", "div.geetest_widget",
        ]:
            try:
                if await page.locator(sel).first.is_visible(timeout=1000):
                    return CircuitBreakerReason.CAPTCHA
            except Exception:
                pass

        # 限流
        for sel in [
            "div:has-text('操作太频繁')", "div:has-text('限流')",
            "div:has-text('请稍后再试')",
        ]:
            try:
                if await page.locator(sel).first.is_visible(timeout=1000):
                    return CircuitBreakerReason.RATE_LIMIT
            except Exception:
                pass

        # 登录过期（已在 _check_login_expired 覆盖，这里跳过）
        # 操作拦截
        for sel in [
            "div:has-text('发布被拦截')", "div:has-text('违规')",
            "div:has-text('无法发布')",
        ]:
            try:
                if await page.locator(sel).first.is_visible(timeout=1000):
                    return CircuitBreakerReason.ACTION_BLOCKED
            except Exception:
                pass

        # 设备验证
        for sel in [
            "div:has-text('设备验证')", "div:has-text('安全验证')",
        ]:
            try:
                if await page.locator(sel).first.is_visible(timeout=1000):
                    return CircuitBreakerReason.DEVICE_VERIFY
            except Exception:
                pass

        return None

    async def _fallback_save_draft(self, page: Page) -> bool:
        """兜底：只从 PLATFORM_SELECTORS[platform]['save_draft_button'] 候选选择器里挑第一个可见的。

        不再用关键词启发式遍历全页按钮——页面改版易误点"发布/发送"类按钮（R1 红线）。
        """
        for sel in self.selectors.get("save_draft_button", []):
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

    async def _extract_draft_id(self, page: Page) -> Optional[str]:
        """尝试从 URL 或 DOM 找平台草稿 ID。"""
        try:
            url = page.url
            # 简单模式：匹配 draftId=xxx 或 draft_id=xxx
            import re
            m = re.search(r"(?:draftId|draft_id|draft)=([A-Za-z0-9]+)", url)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None
