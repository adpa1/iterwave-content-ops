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
            # 小红书图片上传 input 的 accept 是 .jpg,.jpeg,.png,.webp（不含 "image"）
            "input[type='file'][accept*='.jpg']",
            "input[type='file'][accept*='.png']",
            "input[type='file'][accept*='image']",
        ],
        # 图文 tab：默认落在"上传视频"，需先切到"上传图文"才出现图片 input
        "image_tab": [
            "div[class*='tab']:has-text('上传图文')",
        ],
        # P0-2 新增：上传后核验预览卡片数量
        "image_preview_selector": [
            "div.img-list div.img-container",
            "div.img-container",
            "div[class*='image-preview']",
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
            # 2026-10 小红书改版：存草稿按钮文案从"存草稿"改为"暂存离开"
            "button:has-text('暂存离开')",
            "button:has-text('存草稿')",
            "button:has-text('保存草稿')",
            "button.save-draft",
        ],
        # R1 红线：发布按钮严禁点击
        "_publish_button_audit_only": [
            "button:has-text('发布')",
            "button:has-text('发送')",
            "button.publish-btn",
        ],
    },
    Platform.DOUYIN: {
        # 2026-10 真机：默认停在「发布视频」tab，file input 的 accept 全是 video/*；
        # 必须点「发布图文」后才会出现 accept=image/png... 的 input（多图 multiple）。
        "editor_url": "https://creator.douyin.com/creator-micro/content/upload",
        "image_tab": [
            "text=发布图文",
            "div[class*='tab']:has-text('发布图文')",
        ],
        "title": [
            "input[placeholder*='添加作品标题']",
            "input[placeholder*='添加标题']",
            "input[placeholder*='请输入标题']",
        ],
        "body": [
            "div[contenteditable='true'][class*='editor-kit-container']",
            "div[class*='editor-kit-container'] div[contenteditable='true']",
            "div[contenteditable='true']",
        ],
        "image_upload": [
            "input[type='file'][accept*='image/png']",
            "input[type='file'][accept*='image']",
        ],
        "image_preview_selector": [
            "div[class*='cover-item']",
            "div[class*='image-box']",
        ],
        "save_success_selector": [
            "div:has-text('继续编辑')",
            "div:has-text('保存成功')",
            "div:has-text('草稿已保存')",
        ],
        "save_failure_selector": [
            "div:has-text('保存失败')",
            "[class*='error-toast']",
        ],
        "topic_input": [
            "input[placeholder*='添加话题']",
            "input[placeholder*='@ 朋友']",
        ],
        # 抖音图文编辑器没有「存草稿」，底部是「发布 + 暂存离开」；暂存离开=存草稿并离开
        "save_draft_button": [
            "button:has-text('暂存离开')",
            "button:has-text('存草稿')",
            "button:has-text('保存草稿')",
        ],
        # 存草稿成功后回到上传页，顶部出现「你还有上次未发布的图文，是否继续编辑？」
        "save_success_text": ["你还有上次未发布的图文", "继续编辑"],
        # 已有旧草稿时先放弃，保证本次存的是当前内容（不清空用户其它草稿）
        "draft_prompt_discard": ["text=放弃"],
        "_publish_button_audit_only": [
            "button:has-text('发布')",
            "button.publish-btn",
        ],
    },
    Platform.KUAISHOU: {
        # 2026-10 真机：creator.kuaishou.com/content/upload 已失效（会跳 cp.kuaishou.com/profile），
        # 真实发布页是 cp.kuaishou.com/article/publish/video，上传区有「上传视频/上传图文/上传全景视频」tab。
        "editor_url": "https://cp.kuaishou.com/article/publish/video",
        "image_tab": [
            ".ant-tabs-tab-btn:has-text('上传图文')",
            "text=上传图文",
        ],
        # 快手图文的图片 input 用 set_input_files 不触发上传，必须点「上传图片」走 filechooser
        "image_upload_mode": "filechooser",
        "image_upload_trigger": [
            "text=上传图片",
            "button:has-text('上传图片')",
        ],
        "image_upload": [
            "input[type='file'][accept*='image/png']",
        ],
        # 快手图文没有独立标题框（只有描述框），标题并入正文
        "title_optional": True,
        "title": [
            "input[placeholder*='标题']",
            "input[placeholder*='输入标题']",
        ],
        "body": [
            "div[contenteditable='true'][class*='_description']",
            "div[contenteditable='true'][placeholder*='添加合适的话题和描述']",
            "div[contenteditable='true']",
        ],
        # 快手图文没有稳定的逐图预览选择器，用页面文本「N/31」核验（见 _count_uploaded_images）
        "image_preview_selector": [],
        "save_success_selector": [
            "div:has-text('还有上次未发布的图集')",
            "div:has-text('继续编辑')",
            "div:has-text('保存成功')",
        ],
        "save_failure_selector": [
            "div:has-text('保存失败')",
        ],
        # 快手图文编辑页没有「存草稿」：点「取消」离开即自动存为草稿（回到上传页显示「继续编辑」）
        "save_draft_button": [
            "[class*='_button-default']:has-text('取消')",
            "button:has-text('取消')",
            "text=取消",
        ],
        "save_success_text": ["还有上次未发布的图集", "继续编辑"],
        "draft_prompt_discard": ["text=放弃"],
        "_publish_button_audit_only": [
            "button:has-text('发布')",
        ],
    },
    Platform.WECHAT_CHANNELS: {
        # 2026-10 真机：图文入口 = 内容管理 → 图文（列表页 finderNewLifePostList）
        # → 点「发表图文」→ 创作器在 iframe(platform/post/finderNewLifeCreate) 里，
        # 表单实际在它嵌套的子 iframe(micro/content/post/finderNewLifeCreate) 中，
        # 但 page / 父 iframe 的 locator 能直接取到子 iframe 元素（实测可用）。
        "editor_url": "https://channels.weixin.qq.com/platform/post/finderNewLifePostList",
        "image_flow": "wechat_finder",
        "image_create_tab": ["text=发表图文"],
        "composer_frame_suffix": "platform/post/finderNewLifeCreate",
        # 保存草稿后页面不弹 toast、URL 不变，只能去草稿箱按标题核对（实测唯一可靠信号）
        "draft_box_url": "https://channels.weixin.qq.com/platform/post/draftListManager",
        "title": [
            "input[placeholder*='填写标题']",
            "input[placeholder*='标题']",
        ],
        "body": [
            # 描述框是 div.input-editor，contenteditable 属性值为空串（不是 'true'）
            "div.input-editor[contenteditable]",
            "div[contenteditable]",
        ],
        "image_upload": [
            "input[type='file'][accept*='image']",
        ],
        "image_preview_selector": [],
        "save_success_selector": [
            "div:has-text('保存成功')",
            "div:has-text('草稿已保存')",
            "[class*='toast-success']",
        ],
        "save_failure_selector": [
            "div:has-text('保存失败')",
        ],
        "save_draft_button": [
            "button:has-text('保存草稿')",
            "button:has-text('存草稿')",
        ],
        "save_success_text": ["保存成功", "草稿已保存"],
        "_publish_button_audit_only": [
            "button:has-text('发表')",
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
            # 0. 视频号图文走独立链路（入口在「内容管理→图文」列表页，创作器在嵌套 iframe 里）
            if content.image_paths and self.selectors.get("image_flow") == "wechat_finder":
                return await self._save_wechat_image_draft(page, content)

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

            # —— 小红书/抖音/快手：默认停在视频 tab，先切到图文 tab ——
            await self._ensure_image_tab(page, bool(content.image_paths))

            # —— 已有上次未发布的草稿时先「放弃」，保证本次存的是当前内容 ——
            if content.image_paths:
                await self._discard_stale_draft(page)

            # —— 一次性多图上传（小红书/抖音 set_input_files；快手必须走 filechooser）——
            if content.image_paths:
                await self._upload_images(page, content.image_paths)

                # —— 上传后核验预览数量（图片处理需要时间，轮询等待）——
                uploaded = await self._wait_uploaded_images(
                    page, len(content.image_paths), timeout_ms=30000,
                )
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

            # 5. 填标题（快手图文无标题框 → 标题并入正文）
            title_ok = await self.fill_first_visible(page, self.selectors["title"], content.title)
            if not title_ok:
                if not self.selectors.get("title_optional"):
                    return DraftResult(
                        success=False,
                        error_msg="标题输入框未找到",
                    )
                logger.info("平台 %s 无独立标题框，标题并入正文", self.platform)

            # —— P1-1 修复：标签追加到正文末尾（最通用方式，避免各平台标签输入框差异）——
            body_parts = [content.body]
            if content.tags:
                body_parts.append(" ".join(content.tags))
            if not title_ok:
                body_parts.insert(0, content.title)
            body_text = "\n".join(body_parts)

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

            # —— 记录保存前草稿数量（小红书草稿在浏览器本地，保存后数量 +1）——
            initial_draft_count = await self._draft_count(page)

            # 7. ⚠️ 存草稿（只点 save_draft_button，严禁点 _publish_button_audit_only！）
            save_ok = await self.click_first_visible(page, self.selectors["save_draft_button"])
            if not save_ok:
                logger.warning("存草稿按钮未找到，尝试兜底 ...")
                save_ok = await self._fallback_save_draft(page)
            if not save_ok:
                # 小红书底部按钮在 closed shadow DOM（<xhs-publish-btn>）里，
                # Playwright 选择器不可见；调用其 save 回调（只调 _onSave，绝不碰 _onPublish）
                save_ok = await self._call_platform_save_handler(page)

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

            evidence = await self._wait_save_success(
                page, timeout_ms=8000, initial_draft_count=initial_draft_count,
            )
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

    async def _ensure_image_tab(self, page: Page, has_images: bool) -> None:
        """部分平台默认停在视频 tab，需要先切到图文 tab 才会出现图片上传 input。"""
        tabs = self.selectors.get("image_tab") or []
        if not has_images or not tabs:
            return
        if self.selectors.get("image_upload_mode") == "filechooser":
            # 快手：图片 input 在视频 tab 就已挂载（accept 含 png），accept 判断会误判已在图文 tab，
            # 只能用 tab 的 active 态判断。
            try:
                active = await page.evaluate(
                    """() => [...document.querySelectorAll('[class*=tab]')]
                        .some(e => (e.innerText||'').trim().includes('图文')
                                && /active/i.test(e.className || ''))"""
                )
                if active:
                    return
            except Exception:
                pass
        else:
            try:
                inp = page.locator(self.selectors["image_upload"][0])
                accept = ""
                if await inp.count() > 0:
                    accept = (await inp.first.get_attribute("accept")) or ""
                if ".jpg" in accept or ".png" in accept or "image" in accept:
                    return  # 当前已在图文 tab
            except Exception:
                pass
        if await self.click_first_visible(page, tabs, timeout=6000):
            await page.wait_for_timeout(2500)

    async def _save_wechat_image_draft(
        self, page: Page, content: ContentToPublish,
    ) -> DraftResult:
        """视频号图文存草稿：内容管理→图文列表 → 发表图文 → 创作器(嵌套 iframe) 填表 → 保存草稿。

        R1：只点「保存草稿」，永不点「发表」。
        """
        await page.goto(self.selectors["editor_url"], wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        if await self._check_login_expired(page):
            return DraftResult(success=False, error_msg="登录态过期", login_required=True)

        cb_signal = await self._detect_cb_signal(page)
        if cb_signal:
            return DraftResult(
                success=False,
                error_msg=f"检测到熔断信号: {cb_signal.value}",
                cb_signal=cb_signal,
            )

        if not await self.click_first_visible(page, self.selectors["image_create_tab"], timeout=8000):
            return DraftResult(success=False, error_msg="未找到「发表图文」入口")

        # 创作器 iframe：父 iframe(url 以 suffix 结尾) 的 locator 能取到子 iframe 里的表单
        suffix = str(self.selectors["composer_frame_suffix"])
        target = None
        for _ in range(20):
            await page.wait_for_timeout(1000)
            target = next((f for f in page.frames if (f.url or "").endswith(suffix)), None)
            if target is not None:
                break
        if target is None:
            return DraftResult(success=False, error_msg="视频号图文创作器 iframe 未加载")
        await page.wait_for_timeout(3000)

        try:
            await target.locator(self.selectors["image_upload"][0]).first.set_input_files(
                content.image_paths, timeout=30000,
            )
        except Exception as exc:  # noqa: BLE001 — 上传失败要如实回报
            logger.warning("视频号图片上传失败: %s", exc)
            return DraftResult(success=False, error_msg=f"图片上传失败: {exc}"[:200])
        await page.wait_for_timeout(5000)

        await self.fill_first_visible(target, self.selectors["title"], content.title)

        body_parts = [content.body]
        if content.tags:
            body_parts.append(" ".join(content.tags))
        body_text = "\n".join(body_parts)
        body_ok = False
        for sel in self.selectors["body"]:
            try:
                loc = target.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=5000)
                    await loc.fill(body_text, timeout=8000)
                    body_ok = True
                    break
            except Exception:
                continue
        if not body_ok:
            logger.warning("视频号描述框未填入（不影响标题与图片存草稿）")

        if not await self.click_first_visible(target, self.selectors["save_draft_button"], timeout=8000):
            return DraftResult(success=False, error_msg="未找到「保存草稿」按钮")

        evidence = await self._wait_save_success(page, timeout_ms=4000)
        if not evidence:
            evidence = await self._verify_wechat_draft(page, content.title)
        if evidence:
            return DraftResult(
                success=True,
                uploaded_image_count=len(content.image_paths),
                save_evidence=evidence,
                platform_draft_id_missing=True,
            )
        return DraftResult(
            success=False,
            needs_human_verify=True,
            uploaded_image_count=len(content.image_paths),
            error_msg="已点「保存草稿」但未检测到成功信号",
        )

    async def _verify_wechat_draft(self, page: Page, title: str) -> Optional[str]:
        """视频号草稿箱按标题核对（列表在 iframe 里，逐 frame 匹配）。"""
        url = self.selectors.get("draft_box_url")
        key = (title or "").strip()[:10]
        if not url or not key:
            return None
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(8000)
            texts = [await page.evaluate("() => document.body.innerText")]
            for f in page.frames:
                try:
                    # Frame.inner_text 对视频号这类嵌套 iframe 返回空串，evaluate 才拿得到
                    texts.append(await f.evaluate("() => document.body.innerText"))
                except Exception:
                    continue
            for text in texts:
                if text and key in text:
                    return f"draftbox:{key}"
        except Exception as exc:  # noqa: BLE001 — 核对失败不阻断，如实返回 None
            logger.warning("视频号草稿箱核对失败: %s", exc)
        return None

    async def _discard_stale_draft(self, page: Page) -> None:
        """平台提示「还有上次未发布的草稿，是否继续编辑？」时点「放弃」。

        只在本次确实要存新草稿时调用；点的是「放弃」而非「继续编辑」，
        避免把旧草稿的图片带进来导致重复。
        """
        selectors = self.selectors.get("draft_prompt_discard") or []
        if not selectors:
            return
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(1500)
                    logger.info("平台 %s 已放弃上次未发布的草稿", self.platform)
                    return
            except Exception:
                continue

    async def _upload_images(self, page: Page, image_paths: list[str]) -> None:
        """上传图片：快手必须点「上传图片」走 filechooser，其余平台直接 set_input_files。"""
        mode = self.selectors.get("image_upload_mode", "input")
        if mode == "filechooser":
            trigger = self.selectors.get("image_upload_trigger") or []
            for sel in trigger:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() == 0 or not await loc.is_visible():
                        continue
                    async with page.expect_file_chooser(timeout=15000) as fc_info:
                        await loc.click()
                    await (await fc_info.value).set_files(image_paths)
                    return
                except Exception:
                    continue
            # filechooser 全部失败 → 退回 set_input_files（快手实测不触发上传，但至少留个明确错误）
            logger.warning("平台 %s 未找到「上传图片」按钮，退回 set_input_files", self.platform)

        file_input = page.locator(self.selectors["image_upload"][0]).first
        await file_input.set_input_files(image_paths)

    async def _wait_uploaded_images(
        self, page: Page, expected: int, timeout_ms: int = 20000,
    ) -> int:
        """轮询等待图片预览数量达到预期，返回最后一次统计值。"""
        import asyncio

        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        uploaded = 0
        while True:
            uploaded = await self._count_uploaded_images(page)
            if uploaded == expected:
                return uploaded
            if asyncio.get_event_loop().time() >= deadline:
                return uploaded
            await page.wait_for_timeout(500)

    async def _call_platform_save_handler(self, page: Page) -> bool:
        """小红书底部按钮位于 closed shadow DOM，Playwright 无法选择。

        该组件在 light DOM 的 <xhs-publish-btn> 上暴露了 save 回调 `_onSave`。
        ⚠️ R1：只调用保存回调，绝不调用 `_onPublish`。
        """
        if self.platform != Platform.XHS:
            return False
        try:
            ok = await page.evaluate(
                """() => {
                    const el = document.querySelector('xhs-publish-btn');
                    if (!el || typeof el._onSave !== 'function') return false;
                    if (el.getAttribute('save-disabled') === 'true') return false;
                    el._onSave();
                    return true;
                }"""
            )
            if ok:
                logger.info("平台 %s 通过 _onSave 触发存草稿（closed shadow DOM）", self.platform)
            return bool(ok)
        except Exception:
            logger.exception("平台 %s 调用 _onSave 失败", self.platform)
            return False

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
        # 兜底：平台没有稳定预览选择器时，从页面文本里读「已添加 N 张图片」/「1/31」
        import re

        try:
            text = await page.inner_text("body")
        except Exception:
            return 0
        for pattern in (r"已添加\s*(\d+)\s*张图片", r"(\d+)\s*/\s*31", r"(\d+)\s*/\s*35"):
            m = re.search(pattern, text)
            if m:
                return int(m.group(1))
        return 0

    async def _draft_count(self, page: Page) -> Optional[int]:
        """读取页面「草稿箱(n)」数量（小红书草稿存浏览器本地，保存成功即 +1）。"""
        import re

        try:
            body = await page.inner_text("body")
            m = re.search(r"草稿箱\((\d+)\)", body)
            return int(m.group(1)) if m else None
        except Exception:
            return None

    async def _wait_save_success(
        self, page: Page, timeout_ms: int = 8000,
        initial_draft_count: Optional[int] = None,
    ) -> Optional[str]:
        """P0-3：轮询等待平台保存成功信号。

        证据优先级：成功 toast → 草稿箱数量 +1 → URL 跳转。
        超时返回 None。
        """
        import asyncio
        import re

        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            # 配置的 success selector（快查，不阻塞）
            for sel in self.selectors.get("save_success_selector", []):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        try:
                            text = await loc.inner_text() or f"toast:{sel[:40]}"
                        except Exception:
                            text = f"toast_visible:{sel[:40]}"
                        return text[:60].replace("\n", " ")
                except Exception:
                    continue

            # 小红书改版后 toast 文案在 body 文本中
            try:
                body_text = await page.inner_text("body")
            except Exception:
                body_text = ""
            # 平台约定的成功文案（抖音/快手存草稿后回到上传页会提示「继续编辑」）
            markers = ["保存成功", "草稿已保存"]
            markers.extend(self.selectors.get("save_success_text") or [])
            for marker in markers:
                if marker in body_text:
                    return f"body_text:{marker}"

            if initial_draft_count is not None:
                m = re.search(r"草稿箱\((\d+)\)", body_text)
                if m and int(m.group(1)) > initial_draft_count:
                    return f"draft_count:{m.group(1)}"

            if "draft" in page.url.lower():
                return f"url_changed:{page.url[-60:]}"

            await page.wait_for_timeout(400)

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
