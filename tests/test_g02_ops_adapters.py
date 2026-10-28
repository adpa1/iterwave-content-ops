"""G-02：运营适配器「显式失败」与「平台不支持」的边界锁定。

本机无四平台真实登录态，无法验收真实评论/关注是否发出；
但以下两条必须可本机验证，否则就是"假成功"污染 ops_logs（R16/R17）：
  1. 无登录态 → 四平台四个动作一律 NotLoggedInError（绝不 return True / []）
  2. 平台能力不支持（视频号网页端）→ PlatformUnsupportedError，
     且 ops_logs 记 skipped 而不是 failed（不刷告警、不触发熔断）
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.adapters.base import (
    AdapterError,
    NotLoggedInError,
    Platform,
    PlatformUnsupportedError,
)
from src.core.ops.circuit_breaker import CircuitBreakerEngine
from src.core.ops.engine import OperationsEngine


class _NoRhythm:
    def wait_seconds(self, platform, action_type):
        return 0


@pytest.fixture()
def no_login(monkeypatch):
    """强制"无登录态"（适配器内部是函数内 import，故 patch 源模块）。"""
    import src.core.login_manager as lm
    monkeypatch.setattr(lm, "has_login_state", lambda *_a, **_kw: False)


def test_all_platforms_raise_when_not_logged_in(cfg, conn, no_login) -> None:
    """四平台 × 四动作：无登录态必须抛错，不能假成功。"""
    from src.adapters.douyin import DouyinAdapter
    from src.adapters.kuaishou import KuaishouAdapter
    from src.adapters.wechat import WechatChannelsAdapter
    from src.adapters.xhs import XiaohongshuAdapter

    adapters = {
        Platform.XHS: XiaohongshuAdapter(cfg, conn),
        Platform.DOUYIN: DouyinAdapter(cfg, conn),
        Platform.KUAISHOU: KuaishouAdapter(cfg, conn),
        Platform.WECHAT_CHANNELS: WechatChannelsAdapter(cfg, conn),
    }
    for platform, ad in adapters.items():
        with pytest.raises(NotLoggedInError):
            asyncio.run(ad.fetch_new_comments("d1"))
        with pytest.raises(NotLoggedInError):
            asyncio.run(ad.reply_comment("d1", "c1", "内容"))
        with pytest.raises(NotLoggedInError):
            asyncio.run(ad.post_comment("https://example.com/x", "内容"))
        with pytest.raises(NotLoggedInError):
            asyncio.run(ad.follow_user("u1"))


def test_wechat_is_platform_unsupported_not_failure(cfg, conn, monkeypatch) -> None:
    """视频号：有登录态时也应明确声明「平台不支持」而不是伪装成功。"""
    from src.adapters.wechat import WechatChannelsAdapter
    import src.core.login_manager as lm
    monkeypatch.setattr(lm, "has_login_state", lambda *_a, **_kw: True)

    ad = WechatChannelsAdapter(cfg, conn)
    for coro in (
        ad.fetch_new_comments("d1"),
        ad.reply_comment("d1", "c1", "x"),
        ad.post_comment("https://x", "x"),
        ad.follow_user("u1"),
    ):
        with pytest.raises(PlatformUnsupportedError):
            asyncio.run(coro)
    # 必须继承 AdapterError，上层 catch 不会漏
    assert issubclass(PlatformUnsupportedError, AdapterError)


def test_ops_engine_logs_skipped_for_unsupported_platform(cfg, conn) -> None:
    """视频号被动回复：ops_logs 记 skipped，不记 failed。"""

    class _Unsupported:
        async def reply_comment(self, draft_id, comment_id, content):
            raise PlatformUnsupportedError("视频号网页端无评论区")

        async def post_comment(self, target_url, content):
            raise PlatformUnsupportedError("视频号网页端无评论区")

        async def follow_user(self, user_id):
            raise PlatformUnsupportedError("视频号网页端无关注入口")

    engine = OperationsEngine(
        cfg, conn, {Platform.WECHAT_CHANNELS: _Unsupported()},
        CircuitBreakerEngine(cfg, conn, {Platform.WECHAT_CHANNELS: _Unsupported()}),
        _NoRhythm(),
    )
    ok = asyncio.run(
        engine.passive_reply(Platform.WECHAT_CHANNELS, "d1", "c1", "回复内容")
    )
    assert ok is False

    rows = conn.execute(
        "SELECT status, error_msg FROM ops_logs WHERE platform = 'wechat_channels'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "skipped", f"应记 skipped，实际 {rows[0][0]}"
    assert "平台" in rows[0][1] or "视频号" in rows[0][1]

    # failed 计数必须为 0（不触发熔断告警）
    n_failed = conn.execute(
        "SELECT COUNT(*) FROM ops_logs WHERE status = 'failed'"
    ).fetchone()[0]
    assert n_failed == 0


def test_ops_engine_logs_failed_when_login_missing(cfg, conn, no_login) -> None:
    """无登录态：ops_logs 记 failed 并带原因（不允许静默吞掉）。"""
    from src.adapters.douyin import DouyinAdapter

    ad = DouyinAdapter(cfg, conn)
    engine = OperationsEngine(
        cfg, conn, {Platform.DOUYIN: ad},
        CircuitBreakerEngine(cfg, conn, {Platform.DOUYIN: ad}), _NoRhythm(),
    )
    assert asyncio.run(engine.follow(Platform.DOUYIN, "u1")) is False
    row = conn.execute(
        "SELECT status, error_msg FROM ops_logs WHERE platform = 'douyin'"
    ).fetchone()
    assert row is not None and row[0] == "failed"
    assert "登录" in (row[1] or "")
