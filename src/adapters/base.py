"""
平台适配器抽象基类 — 统一四平台的外部操作接口。

所有有副作用的外部操作（存草稿 / 数据采集 / 评论 / 关注）都走这里。
实现类必须严格遵守 R1：只存草稿，**严禁点发布按钮**。

复用来源（R4/R6.5，复制而非外部调用）：
- login_state.py / login_qr.py    → 已复制到 src/vendor/easel/
- platform_readback.py            → 已复制到 src/vendor/easel/（find_published_work 等）
- account_stats.py                → 已复制到 src/vendor/easel/
- web_publisher.py 的工具函数     → 按需复制进 src/adapters/（当前只实现存草稿分支）
- xhs_publish.py / douyin_publish.py → 参考后写草稿版
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# =====================================================================
# 枚举值常量
# =====================================================================

class Platform(str, Enum):
    XHS = "xhs"
    DOUYIN = "douyin"
    KUAISHOU = "kuaishou"
    WECHAT_CHANNELS = "wechat_channels"

    @property
    def display_name(self) -> str:
        return {
            Platform.XHS: "小红书",
            Platform.DOUYIN: "抖音",
            Platform.KUAISHOU: "快手",
            Platform.WECHAT_CHANNELS: "视频号",
        }[self]


class DraftStatus(str, Enum):
    """单篇草稿状态（R7/R14，必须与 DB CHECK 完全一致）。"""
    DRAFT_PENDING = "draft_pending"
    DRAFT_SAVED = "draft_saved"
    USER_PUBLISHED = "user_published"
    DATA_FETCHING = "data_fetching"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class FetchStatus(str, Enum):
    """数据采集五态（R3/R14）。"""
    PENDING = "pending"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    PARSE_ERROR = "parse_error"


# 创作者中心首页（R6 三步恢复只读探测用）
_PLATFORM_HOME: dict["Platform", str] = {
    Platform.XHS: "https://creator.xiaohongshu.com/new/home",
    Platform.DOUYIN: "https://creator.douyin.com/creator-micro/home",
    Platform.KUAISHOU: "https://cp.kuaishou.com/profile",
    Platform.WECHAT_CHANNELS: "https://channels.weixin.qq.com/platform",
}

_LOGIN_URL_RE = re.compile(r"(passport|/login|login\.)", re.IGNORECASE)


async def _is_login_page(url: str, page) -> bool:
    """判断当前页是否被踢到登录页（URL + 常见登录控件双判）。

    page 是 **async** Playwright Page：`query_selector_all` / `is_visible` 都必须 await，
    否则拿到的是未 await 的 coroutine（旧实现会静默失效，登录墙判断永远 False）。
    """
    if _LOGIN_URL_RE.search(url or ""):
        return True
    try:
        selectors = 'button:has-text("扫码登录"), [class*="login-btn"], [class*="login-box"]'
        for el in await page.query_selector_all(selectors):
            if await el.is_visible():
                return True
        return False
    except Exception:  # noqa: BLE001 — DOM 查询失败不阻塞判断
        return False


class AdapterError(RuntimeError):
    """平台适配器执行失败（非预期，需人工介入）。"""


class NotLoggedInError(AdapterError):
    """无登录态 / 登录态失效 —— 必须显式失败，禁止假成功污染 ops_logs（G-02）。"""


class PlatformUnsupportedError(AdapterError):
    """平台侧根本不支持该动作（不是"我们还没写"，是平台没这个入口）。

    例如视频号的评论互动只在微信客户端内，网页端（channels.weixin.qq.com）
    没有评论区。这类动作应记 ops_logs.status='skipped'，不当成 failed，
    否则每天定时任务都会刷失败告警（R16/R17）。
    """


class CircuitBreakerReason(str, Enum):
    """熔断 5 信号（R6）。"""
    CAPTCHA = "captcha"
    RATE_LIMIT = "rate_limit"
    LOGIN_EXPIRED = "login_expired"
    ACTION_BLOCKED = "action_blocked"
    DEVICE_VERIFY = "device_verify"


# =====================================================================
# 数据模型（全部 Optional[int] = None，R3 NULL ≠ 0）
# =====================================================================

class DraftResult:
    """存草稿结果 — 三种语义严格区分（P0-3 / P1-1 修复后）：

    1. ``success=True``          平台确认保存 ✅（检测到 toast / 页面跳转 / 草稿列表）
    2. ``needs_human_verify=True`` 按钮点了但无成功证据（R17），需人工核实
    3. ``success=False``         明确失败（登录态过期 / 熔断 / 图片上传数量不符 / 按钮未找到 ...）
    """
    def __init__(
        self,
        success: bool,
        platform_draft_id: Optional[str] = None,
        error_msg: Optional[str] = None,
        login_required: bool = False,
        cb_signal: "Optional[CircuitBreakerReason]" = None,
        # —— P0-3 / R17 新增 ——
        needs_human_verify: bool = False,
        platform_draft_id_missing: bool = False,
        uploaded_image_count: int = 0,
        save_evidence: Optional[str] = None,
    ):
        self.success = success
        self.platform_draft_id = platform_draft_id
        self.error_msg = error_msg
        self.login_required = login_required
        # R6：页面检测到熔断信号时由调用方（round_executor / ops engine）上报熔断器
        self.cb_signal = cb_signal
        self.needs_human_verify = needs_human_verify
        self.platform_draft_id_missing = platform_draft_id_missing
        self.uploaded_image_count = uploaded_image_count
        self.save_evidence = save_evidence

    def __repr__(self) -> str:
        parts = [f"success={self.success}"]
        if self.needs_human_verify:
            parts.append("needs_human_verify=YES")
        if self.platform_draft_id:
            parts.append(f"draft_id={self.platform_draft_id}")
        elif self.platform_draft_id_missing:
            parts.append("draft_id=MISSING")
        if self.save_evidence:
            parts.append(f"evidence={self.save_evidence}")
        if self.error_msg:
            parts.append(f"error={self.error_msg}")
        return f"<DraftResult {' '.join(parts)}>"


class MetricsSnapshot:
    """采集到的平台数据指标。

    R3 红线：所有数值字段 Optional[int] = None。
    - None  = 平台没返回该字段 / 采集失败
    - 0     = 平台明确返回 0（真实零值）
    归一化比率计算时 views=None/0 → 比率全部 NULL，不做除法。
    """
    def __init__(
        self,
        platform: Platform,
        fetch_status: FetchStatus = FetchStatus.PENDING,
        fetched_at: Optional[datetime] = None,
        likes: Optional[int] = None,
        collects: Optional[int] = None,
        comments: Optional[int] = None,
        shares: Optional[int] = None,
        views: Optional[int] = None,
        new_followers: Optional[int] = None,
        raw_response: Optional[str] = None,
        error_msg: Optional[str] = None,
    ):
        self.platform = platform
        self.fetch_status = fetch_status
        self.fetched_at = fetched_at or datetime.now()
        self.likes = likes
        self.collects = collects
        self.comments = comments
        self.shares = shares
        self.views = views
        self.new_followers = new_followers
        self.raw_response = raw_response
        self.error_msg = error_msg

    # --- 归一化比率（R3：依赖字段任一 NULL → 该比率为 NULL，绝不 `or 0` 造数）---

    @property
    def interaction_rate(self) -> Optional[float]:
        """(likes + collects + comments) / views，shares 不计入（§14.2）。

        抖音 / 快手作品读回没有「收藏」字段：这两个平台按 (likes + comments) / views 计算，
        各平台只在自己内部比较（R13），口径差异不影响平台内归一化。
        """
        if not self.views or self.views <= 0:
            return None
        if None in (self.likes, self.comments):
            return None
        if self.collects is None:
            if getattr(self.platform, "value", self.platform) in ("douyin", "kuaishou"):
                return (self.likes + self.comments) / self.views
            return None
        return (self.likes + self.collects + self.comments) / self.views

    @property
    def like_rate(self) -> Optional[float]:
        if not self.views or self.views <= 0 or self.likes is None:
            return None
        return self.likes / self.views

    @property
    def collect_rate(self) -> Optional[float]:
        if not self.views or self.views <= 0 or self.collects is None:
            return None
        return self.collects / self.views

    @property
    def follower_efficiency(self) -> Optional[float]:
        """涨粉效率 = new_followers / views。"""
        if not self.views or self.views <= 0 or self.new_followers is None:
            return None
        return self.new_followers / self.views


class ContentToPublish:
    """待存草稿的内容。"""
    def __init__(
        self,
        content_id: str,
        platform: Platform,
        title: str,
        body: str,
        image_paths: list[str],
        tags: Optional[list[str]] = None,
    ):
        self.content_id = content_id
        self.platform = platform
        self.title = title
        self.body = body
        self.image_paths = image_paths
        self.tags = tags or []


# =====================================================================
# PlatformAdapter 抽象基类
# =====================================================================

class PlatformAdapter(ABC):
    """四平台统一接口 — 每个平台实现这些方法。

    R1 红线：save_draft 只存草稿，**严禁点发布按钮**。
    发布只能由用户手动点"发布"后确认。
    """

    platform: Platform

    def __init__(self, cfg, db_conn):
        self.cfg = cfg
        self.db = db_conn
        self._browser_lock = None  # asyncio.Lock，由 scheduler 注入

    # ------------------------------------------------------------------
    # 存草稿（R1 红线！严禁点发布按钮）
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_draft(self, content: ContentToPublish) -> DraftResult:
        """填表单 → 点击"存草稿"按钮。

        **绝对不能调用点击"发布" / "发送" / "提交"按钮的路径。**
        返回 DraftResult：success + platform_draft_id（如果平台返回）。
        """

    # ------------------------------------------------------------------
    # 数据采集
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_metrics(self, platform_draft_id: Optional[str], title: str) -> MetricsSnapshot:
        """从创作者中心读回已发布笔记的数据。

        draft_id 可能为空（部分平台 readback 只按标题+时间窗匹配）。
        """

    async def _fetch_metrics_via_readback(
        self, platform_draft_id: Optional[str], title: str,
    ) -> MetricsSnapshot:
        """四平台共用的真实读回实现（用 src/vendor/easel/ 的复制件，R4）。

        R3：解析不到的字段为 None；一个数字都没有 → parse_error，绝不补 0。
        """
        import asyncio
        import json

        from .readback import (
            ReadbackUnavailable, failed_snapshot, match_work,
            read_works_sync, snapshot_from_work,
        )

        try:
            # vendor 读回是同步实现：放到线程里用独立 sync Playwright 跑，
            # 不能把 async Page 传进去（只会得到未 await 的 coroutine）。
            works = await asyncio.to_thread(read_works_sync, self.platform)
        except ReadbackUnavailable as exc:
            logger.warning("读回不可用 platform=%s: %s", self.platform.value, exc)
            return failed_snapshot(self.platform, str(exc))
        except Exception as exc:  # noqa: BLE001 — 登录失效 / 网络故障统一落 failed
            logger.exception("读回失败 platform=%s", self.platform.value)
            return failed_snapshot(self.platform, f"读回失败: {exc}")

        if not works:
            return failed_snapshot(
                self.platform, "未读到作品列表（可能登录态失效或暂无已发布内容）",
            )

        work = match_work(works, title, platform_draft_id)
        if work is None:
            return failed_snapshot(
                self.platform, f"作品列表中未匹配到标题「{(title or '')[:20]}」",
            )

        return snapshot_from_work(
            self.platform, work, json.dumps(work, ensure_ascii=False),
        )

    # ------------------------------------------------------------------
    # 登录态检查
    # ------------------------------------------------------------------

    @abstractmethod
    async def test_login(self) -> bool:
        """检查当前浏览器登录态是否有效（不做任何写操作）。"""

    @abstractmethod
    async def test_lightweight_action(self) -> bool:
        """读取创作者中心首页成功（不做写操作）。

        R6 熔断恢复三步验证之第三步。
        """

    # ------------------------------------------------------------------
    # 登录态真实探测（R6 三步恢复，禁止无条件 return True）
    # ------------------------------------------------------------------

    async def _probe_creator_home(self, require_content: bool) -> tuple[bool, str]:
        """打开创作者中心首页做只读探测。

        require_content=False → 只判断是否被踢到登录页（test_login 用）
        require_content=True  → 还要求首页有实质内容（test_lightweight_action 用）
        """
        from . import new_context_for_platform
        from ..core.login_manager import has_login_state

        home = _PLATFORM_HOME.get(self.platform)
        if not home:
            return False, f"未知平台: {self.platform.value}"

        # 没有登录态文件 → 直接判失败，不必启动浏览器（避免每次弹窗）
        if not has_login_state(self.platform):
            return False, f"{self.platform.display_name} 无登录态文件，请先扫码登录"

        try:
            async with new_context_for_platform(self.platform) as ctx:
                page = await ctx.new_page()
                try:
                    await page.goto(home, wait_until="domcontentloaded", timeout=20000)
                    # SPA 首屏异步渲染：视频号实测要 6~8s body 才非空，固定 1.5s 会误判登录失效。
                    try:
                        await page.wait_for_function(
                            "() => document.body && document.body.innerText.trim().length >= 50",
                            timeout=10000,
                        )
                    except Exception:  # noqa: BLE001 — 超时交给下方 body 长度判定
                        pass
                    await page.wait_for_timeout(500)
                    url = page.url or ""

                    if await _is_login_page(url, page):
                        return False, f"登录态失效（跳转登录页 {url[:80]}）"

                    if not require_content:
                        return True, f"登录态有效（{url[:80]}）"

                    body = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    ) or ""
                    if len(body.strip()) < 50:
                        return False, "首页内容为空，疑似登录态失效或页面结构变化"
                    return True, f"首页可读（{len(body.strip())} 字符）"
                finally:
                    await page.close()
        except Exception as exc:  # noqa: BLE001 — 探测异常一律视为未通过
            logger.warning("创作者中心探测异常 platform=%s: %s", self.platform.value, exc)
            return False, f"探测异常: {exc}"

    # ------------------------------------------------------------------
    # 运营动作（全部幂等，R16）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 运营（评论 / 关注）
    # ------------------------------------------------------------------

    def _require_login(self) -> None:
        """运营动作的前置闸门（G-02）：无登录态直接抛错，绝不返回假成功。

        ops/engine.py 会把该异常落成 ops_logs.status='failed'，
        并把失败原因写进 error_msg，避免用 True/None 掩盖真实失败。
        """
        from src.core.login_manager import has_login_state

        if not has_login_state(self.platform):
            raise NotLoggedInError(
                f"{self.platform.value} 无登录态，无法执行运营动作；"
                f"请先在「系统设置」扫码登录后再试"
            )

    async def _get_browser_context(self):
        """运营动作用的 BrowserContext —— **必须注入登录态**（G-02）。

        用 adapters.new_context_for_platform，它会从 data/login_states/{platform}.json
        恢复 storage_state。此前的子类实现都是空的 new_context()，导致即使已登录也会
        被平台踢到登录页，运营动作必然失败。
        """
        from . import new_context_for_platform

        return new_context_for_platform(self.platform)

    @abstractmethod
    async def fetch_new_comments(self, draft_id: str) -> list[dict]:
        """拉取某篇已发布草稿的最新评论列表。

        每项形如 {"comment_id": "...", "user_id": "...", "content": "...", "timestamp": ...}。
        底层实现可以直接返回空列表（无登录态时），comment_polling 会兜底。
        """

    @abstractmethod
    async def reply_comment(self, draft_id: str, comment_id: str, content: str) -> bool:
        """被动回复评论。回复前查 ops_logs 幂等。"""

    @abstractmethod
    async def post_comment(self, target_url: str, content: str) -> Optional[str]:
        """主动评论。返回 comment_id 或 None（pending_verify 场景）。"""

    @abstractmethod
    async def follow_user(self, user_id: str) -> bool:
        """关注用户。"""

    # ------------------------------------------------------------------
    # 熔断器（检测 5 种触发信号）
    # ------------------------------------------------------------------

    async def detect_circuit_breaker_signal(self) -> Optional[CircuitBreakerReason]:
        """检测当前页面是否出现熔断信号。

        默认实现：在 save_draft / fetch_metrics 过程中各平台需主动调用
        并返回 CircuitBreakerReason 或 None。子类如不 override 则依赖
        各方法内部的 try-except + 信号判断。
        """
        return None
