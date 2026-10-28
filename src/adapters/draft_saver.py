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
import os
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
        # P0-2 新增：上传后核验预览卡片数量
        "image_preview_selector": [
            "div[class*='cover-item']",
            "div[class*='image-preview']",
            "div:has(img) [class*='uploaded']",
        ],
        # P0-3 新增：平台保存成功信号
        "save_success_selector": [
            "div:has-text('保存成功')",
            "div:has-text('草稿已保存')",
            "[class*='toast-success']",
            "[class*='success-toast']",
        ],
        # P0-3 新增：保存失败信号（更快识别）
        "save_failure_selector": [
            "div:has-text('保存失败')",
            "div:has-text('上传失败')",
            "div:has-text('发布失败')",
            "[class*='error-toast']",
            "[class*='fail-toast']",
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
        "image_preview_selector": [
            "div[class*='cover-item']",
            "div[class*='image-box']",
        ],
        "save_success_selector": [
            "div:has-text('保存成功')",
            "div:has-text('草稿已保存')",
            "[class*='toast-success']",
        ],
        "save_failure_selector": [
            "div:has-text('保存失败')",
            "[class*='error-toast']",
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
        "image_preview_selector": [
            "div[class*='cover-item']",
        ],
        "save_success_selector": [
            "div:has-text('保存成功')",
            "[class*='toast-success']",
        ],
        "save_failure_selector": [
            "div:has-text('保存失败')",
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
        "image_preview_selector": [
            "div[class*='cover-item']",
        ],
        "save_success_selector": [
            "div:has-text('保存成功')",
            "div:has-text('草稿已保存')",
            "[class*='toast-success']",
        ],
        "save_failure_selector": [
            "div:has-text('保存失败')",
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

        流程：goto → 等加载 → 一次性多图上传 + 数量核验 → 填标题 → 填正文
              → 标签追加正文 → 点存草稿 → 等成功信号 → 提取平台 draft_id。

        **永不点击发布按钮**（R1）。

        成功判定（P0-3 修复）：
          ✅ 成功证据出现（toast / 页面跳转 / URL 变 / 草稿列表）→ success=True
          ⚠️ 按钮点了但无成功证据 → needs_human_verify=True（R17）
          ❌ 明确失败（登录态 / 熔断 / 图片数量不符 / 按钮未找到）→ success=False
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

            # —— P0-2 修复：先校验图片路径存在性 ——
            missing = [p for p in content.image_paths if not os.path.isfile(p)]
            if missing:
                return DraftResult(
                    success=False,
                    error_msg=f"图片文件不存在: {missing[0]} (共 {len(missing)} 个缺失)",
                )

            # —— P0-2 修复：一次性多图上传（不做 is_visible 检查，Playwright file input 隐藏也能 set_input_files）——
            if content.image_paths:
                file_input = page.locator(self.selectors["image_upload"][0]).first
                await file_input.set_input_files(content.image_paths)
                await page.wait_for_timeout(2000)

                # —— P0-2 修复：上传后核验预览数量 ——
                uploaded = await self._count_uploaded_images(page)
                if uploaded != len(content.image_paths):
                    logger.warning(
                        "平台 %s 图片数量不符: 预期 %d / 实际 %d",
                        self.platform, len(content.image_paths), uploaded,
                    )
                    return DraftResult(
                        success=False,
                        error_msg=f"图片上传数量不符: 预期 {len(content.image_paths)} / 实际 {uploaded}",
                        uploaded_image_count=uploaded,
                    )

            # 5. 填标题
            title_ok = await self.fill_first_visible(page, self.selectors["title"], content.title)
            if not title_ok:
                return DraftResult(
                    success=False,
                    error_msg="标题输入框未找到",
                )

            # —— P1-1 修复：标签追加到正文末尾（最通用方式，避免各平台标签输入框差异）——
            body_text = content.body
            if content.tags:
                tag_text = " " + " ".join(content.tags)
                body_text = content.body + tag_text

            # 6. 填正文（可能已追加标签）
            body_ok = await self.fill_first_visible(page, self.selectors["body"], body_text)
            if not body_ok:
                return DraftResult(
                    success=False,
                    error_msg="正文输入框未找到",
                )

            # —— P1-1 额外：如平台有独立话题输入框，也填一次 ——
            topic_sels = self.selectors.get("topic_input", [])
            if topic_sels and content.tags:
                await self.fill_first_visible(page, topic_sels, " ".join(content.tags))

            # 7. ⚠️ 存草稿（只点 save_draft_button，严禁点 _publish_button_audit_only！）
            save_ok = await self.click_first_visible(page, self.selectors["save_draft_button"])
            if not save_ok:
                logger.warning("存草稿按钮未找到，尝试兜底 ...")
                save_ok = await self._fallback_save_draft(page)

            if not save_ok:
                return DraftResult(
                    success=False,
                    error_msg="存草稿按钮未找到",
                )

            # —— P0-3 修复：等待平台成功信号（最多 8s）——
            failure_evidence = await self._wait_save_failure(page, timeout_ms=1500)
            if failure_evidence:
                return DraftResult(
                    success=False,
                    error_msg=f"平台反馈保存失败: {failure_evidence}",
                )

            evidence = await self._wait_save_success(page, timeout_ms=8000)
            draft_id = await self._extract_draft_id(page)

            if evidence is None:
                # —— P0-3 核心：按钮点了但无成功证据 → needs_human_verify（R17）——
                logger.warning(
                    "平台 %s 存草稿无成功证据 content_id=%s → needs_human_verify",
                    self.platform, content.content_id,
                )
                return DraftResult(
                    success=False,
                    needs_human_verify=True,
                    error_msg="点击存草稿后未检测到平台成功信号，请人工核实",
                    platform_draft_id=draft_id,
                    platform_draft_id_missing=(draft_id is None),
                    uploaded_image_count=len(content.image_paths),
                )

            # —— 成功证据出现 ✅ ——
            logger.info(
                "✅ 平台 %s 存草稿成功 content_id=%s draft_id=%s evidence=%s",
                self.platform, content.content_id, draft_id, evidence,
            )
            return DraftResult(
                success=True,
                platform_draft_id=draft_id,
                platform_draft_id_missing=(draft_id is None),
                save_evidence=evidence,
                uploaded_image_count=len(content.image_paths),
            )

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

    async def _count_uploaded_images(self, page: Page) -> int:
        """P0-2：计算实际上传成功的图片预览卡片数量。"""
        for sel in self.selectors.get("image_preview_selector", []):
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    return count
            except Exception:
                continue
        return 0

    async def _wait_save_success(self, page: Page, timeout_ms: int = 8000) -> Optional[str]:
        """P0-3：等待平台保存成功信号。返回成功证据字符串（toast 文本 / 跳转迹象 / URL 变），
        超时返回 None。"""
        import asyncio

        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000

        # 检查 URL 是否含了 draft_id（保存后页面跳转）
        initial_url = page.url

        # 优先检查配置的 success selector
        for sel in self.selectors.get("save_success_selector", []):
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                try:
                    return await loc.inner_text() or f"toast:{sel[:40]}"
                except Exception:
                    return f"toast_visible:{sel[:40]}"
            except Exception:
                continue

        # URL 变化也算成功证据（保存后可能跳转）
        try:
            await page.wait_for_url(f"**/*draft*", timeout=min(3000, timeout_ms))
            if page.url != initial_url:
                return f"url_changed:{page.url[-60:]}"
        except Exception:
            pass

        return None

    async def _wait_save_failure(self, page: Page, timeout_ms: int = 1500) -> Optional[str]:
        """P0-3：快速检测保存失败信号，先于成功信号判断。返回失败文本或 None。"""
        for sel in self.selectors.get("save_failure_selector", []):
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                try:
                    return await loc.inner_text() or sel[:40]
                except Exception:
                    return sel[:40]
            except Exception:
                continue
        return None

    async def _extract_draft_id(self, page: Page) -> Optional[str]:
        """P0-3 增强：URL query 参数优先，DOM 兜底。"""
        import re

        # 1. URL query 参数
        try:
            url = page.url
            m = re.search(r"(?:draftId|draft_id|draft|note_id|noteId)=([A-Za-z0-9]+)", url)
            if m:
                return m.group(1)
        except Exception:
            pass

        # 2. DOM 兜底：从 toast / 跳转后的隐藏 input / meta 标签提取
        try:
            candidates = [
                "#draftId", "[name='draftId']", "[data-draft-id]",
                "meta[property='og:url']",
            ]
            for sel in candidates:
                loc = page.locator(sel).first
                try:
                    if await loc.count() > 0:
                        val = await loc.input_value() or await loc.get_attribute("content") or ""
                        if val:
                            m2 = re.search(r"([A-Za-z0-9]{8,})", val)
                            if m2:
                                return m2.group(1)
                except Exception:
                    continue
        except Exception:
            pass

        return None
