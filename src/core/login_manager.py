"""
Playwright 登录态管理 — 用 storage_state.json 恢复（Easel 同款方式）。

存储位置：data/login_states/{platform}.json（Playwright storage_state 格式）
    {
        "cookies": [{"name": "...", "value": "...", "domain": "...", "path": "...", "expires": ...}],
        "origins": [...]
    }

生成方式（二选一）：
1. 本模块的 login_with_qr() — 启动浏览器出二维码，用户扫码后手动保存 storage_state
2. 手动 `browser.contexts[0].storage_state(path="...")` — 已有登录态的浏览器保存

R23 安全：
- 文件权限 chmod 600
- 目录 chmod 700
- 不进 git（.gitignore 已排除）
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Optional

from playwright.async_api import Browser, BrowserContext, async_playwright

from ..adapters.base import Platform
from .config import PROJECT_ROOT

logger = logging.getLogger(__name__)

LOGIN_DIR = PROJECT_ROOT / "data" / "login_states"
_TENANT_LOGIN_DIR: ContextVar[Path | None] = ContextVar("iterwave_tenant_login_dir", default=None)


def configure_data_dir(data_dir: Path | str) -> None:
    """兼容前台扫码登录：设置当前交互账号组目录。

    后台 Agent 任务必须使用 tenant_login_dir() 显式绑定目录，不得依赖此进程级默认值。
    """
    global LOGIN_DIR
    LOGIN_DIR = Path(data_dir).resolve() / "login_states"


@contextmanager
def tenant_login_dir(data_dir: Path | str) -> Iterator[None]:
    """将 storage_state 目录绑定到当前异步任务上下文，退出时自动恢复。

    ContextVar 可随 asyncio Task 隔离，避免账号组切换覆盖正在执行任务的登录态路径。
    """
    token = _TENANT_LOGIN_DIR.set(Path(data_dir).resolve() / "login_states")
    try:
        yield
    finally:
        _TENANT_LOGIN_DIR.reset(token)


def _effective_login_dir() -> Path:
    return _TENANT_LOGIN_DIR.get() or LOGIN_DIR


def _platform_state_path(platform: Platform, data_dir: Path | str | None = None) -> Path:
    """某平台 storage_state.json 的路径；显式 data_dir 优先于异步上下文。"""
    login_dir = Path(data_dir).resolve() / "login_states" if data_dir is not None else _effective_login_dir()
    return login_dir / f"{platform.value}.json"


def has_login_state(platform: Platform, data_dir: Path | str | None = None) -> bool:
    """某平台是否已有登录态；API 前台查询应显式传入活动账号组目录。"""
    return _platform_state_path(platform, data_dir=data_dir).exists()


def background_headless() -> bool:
    """后台任务（拉数据/评论轮询/存草稿/熔断探测）默认无头，不弹窗口。

    调试时设 `ITERWAVE_BROWSER_HEADED=1` 临时改回有头；扫码登录不走这里，
    始终有头（用户要看见二维码）。
    """
    return os.environ.get("ITERWAVE_BROWSER_HEADED", "").strip().lower() not in (
        "1", "true", "yes", "on",
    )


async def get_or_create_context(browser: Browser, platform: Platform) -> BrowserContext:
    """为某平台创建 BrowserContext，恢复登录态（如有）。

    注意：Playwright async API 的 new_context() 是协程，必须 await。
    """
    state_path = _platform_state_path(platform)
    if state_path.exists():
        logger.info("从 storage_state 恢复 %s 登录态", platform.value)
        return await browser.new_context(storage_state=str(state_path))
    logger.warning("⚠️ %s 无登录态，会弹扫码窗口。请先运行 `login_with_qr` 登录。", platform.value)
    return await browser.new_context()


def save_context_state(context: BrowserContext, platform: Platform) -> Path:
    """把当前 context 的 storage_state 保存到文件。"""
    login_dir = _effective_login_dir()
    login_dir.mkdir(parents=True, exist_ok=True)
    path = _platform_state_path(platform)
    context.storage_state(path=str(path))
    # R23 安全：chmod 600 / 700
    os.chmod(login_dir, 0o700)
    os.chmod(path, 0o600)
    logger.info("✅ %s 登录态已保存: %s (权限 600)", platform.value, path)
    return path


async def login_with_qr(platform: Platform, login_url: str,
                        wait_timeout: int = 300) -> Optional[Path]:
    """启动 headed 浏览器让用户扫码登录，成功后保存 storage_state。

    返回保存的 state 文件路径（登录成功）或 None（超时/用户取消）。
    """
    print(f"\n🔐 正在启动浏览器登录 {platform.display_name} ...")
    print(f"   登录地址: {login_url}")
    print(f"   请在弹出的浏览器中扫码登录，登录完成后回到此终端按 Enter 继续")
    print(f"   （或等待 {wait_timeout}s 自动超时）\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

        # 等用户登录
        try:
            # 给用户时间扫码，终端等待 Enter
            import sys
            import select
            print("⏳ 等待扫码中... 登录完成后按 Enter 键保存")
            print(f"   （最多等 {wait_timeout} 秒）")

            import asyncio
            loop = asyncio.get_event_loop()

            async def wait_for_enter(timeout):
                future = loop.run_in_executor(None, select.select, [sys.stdin], [], [], timeout)
                r, _, _ = await future
                return bool(r)

            await asyncio.wait_for(wait_for_enter(wait_timeout), timeout=wait_timeout)
        except asyncio.TimeoutError:
            print("❌ 超时，未收到 Enter 确认")
            await browser.close()
            return None

        # 保存 storage_state
        path = save_context_state(ctx, platform)
        await browser.close()
        print(f"\n✅ 登录态已保存到 {path}")
        return path
