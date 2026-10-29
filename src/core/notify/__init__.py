"""
消息通知 — 7 场景 × 4 级 × 多通道（macOS / 企业微信 / 飞书）。

设计要点（设计方案 §八）：
- 通道可配：macOS 原生通知默认开启，企微/飞书 webhook 通过 .env 提供
- 事件可配：config.notifications.events 控制每个场景是否发送 + 默认级别
- 全部落 notifications 表留痕，用于 R16 幂等去重与审计
- webhook URL 视同密钥（R23）：不写日志、不进 API 响应
- 通知正文不得含 cookie / token（R23），发送前统一脱敏
- 时间一律 datetime.now()（R5）

用法：
    from src.core.notify import Notifier
    notifier = Notifier(cfg, conn)
    notifier.emit("draft_failed", "小红书存草稿失败：XXX", platform="xhs")
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta
from typing import Optional


logger = logging.getLogger(__name__)

# 4 级（与 notifications.level CHECK 一致）
LEVELS = ("info", "warning", "error", "critical")

# 事件目录：event → (DB type, 默认级别, 中文标题)
# DB type 与 notifications.type CHECK 一致（含 003 迁移扩展的 3 个）
_EVENT_CATALOG: dict[str, tuple[str, str, str]] = {
    # 设计方案 §八 的 7 个场景
    "draft_saved": ("draft_saved", "info", "草稿已保存"),
    "draft_failed": ("draft_failed", "error", "存草稿失败"),
    "circuit_broken": ("circuit_broken", "critical", "平台熔断"),
    "login_expired": ("login_expired", "warning", "登录态过期"),
    "data_fetch_failed": ("data_fetch_failed", "error", "数据拉取失败"),
    "goal_completed": ("goal_completed", "info", "目标达成"),
    "round_completed": ("round_completed", "info", "轮次复盘完成"),
    # 运营过程中额外需要的 4 个
    "circuit_recovered": ("circuit_recovered", "info", "熔断恢复"),
    "publish_confirmed": ("publish_confirmed", "info", "发布已确认"),
    "quota_exhausted": ("quota_exhausted", "info", "今日运营配额用完"),
    "round_timeout": ("round_timeout", "warning", "轮次阶段超时"),
}

_APP_TITLE = "四平台运营 Agent"

# R23 脱敏工具：统一走 src.core.redact（避免多模块各写一份）
from ..redact import redact as _redact  # noqa: F401 — 对外兼容 _redact 名字


def _as_escape(text: str) -> str:
    """转义 AppleScript 字符串字面量，避免通知文本中的换行逃逸出字符串。"""
    text = str(text or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return text.replace("\\", "\\\\").replace('"', '\\"')

def macos_available() -> bool:
    """macOS 通知只在 macOS 上可用；Linux/Windows 部署自动跳过（可移植）。"""
    import shutil
    import sys

    return sys.platform == "darwin" and shutil.which("osascript") is not None


class Notifier:
    """多通道通知器 — 单例，由 lifespan 创建后注入 executor / scheduler。"""

    def __init__(self, cfg, conn, dedupe_window_minutes: int = 30):
        self.cfg = cfg
        self.conn = conn
        self.dedupe_window_minutes = dedupe_window_minutes

        ncfg = cfg.notifications
        self.channels = ncfg.channels
        # config 里声明的事件配置（enabled / level）
        self.event_cfg = {e.name: e for e in ncfg.events}

    def _rule_override(self, event: str) -> Optional[list[tuple[str, str, int]]]:
        """notify_rules 表覆盖（页面可编辑）。

        返回 [(channel, level, enabled)]；表不存在或没有该事件的规则 → None（走 config 默认）。
        """
        try:
            rows = self.conn.execute(
                "SELECT channel, level, enabled FROM notify_rules WHERE scenario = ?",
                (event,),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        return rows or None

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def emit(
        self,
        event: str,
        message: str,
        level: Optional[str] = None,
        platform: Optional[str] = None,
        title: Optional[str] = None,
    ) -> bool:
        """发送一条通知。返回是否至少投递成功一个通道。

        - event 未在目录中 / 配置里 disabled → 跳过（返回 False）
        - warning/error/critical 在去重窗口内同 type+platform+正文只发一次（R16）
        - 任何通道异常都不向上抛，只记日志
        """
        catalog = _EVENT_CATALOG.get(event)
        if catalog is None:
            logger.warning("未知通知事件，已忽略: %s", event)
            return False

        db_type, default_level, default_title = catalog
        ecfg = self.event_cfg.get(event)
        if ecfg is not None and not ecfg.enabled:
            logger.debug("通知事件已禁用，跳过: %s", event)
            return False

        final_level = level or (ecfg.level if ecfg and ecfg.level else default_level)
        if final_level not in LEVELS:
            logger.warning("非法通知级别 %s，回退默认 %s", final_level, default_level)
            final_level = default_level

        # notify_rules 表覆盖（页面可编辑）：有规则 → 按规则限定通道 / 级别
        rules = self._rule_override(event)
        allowed_channels: Optional[set[str]] = None
        if rules is not None:
            active = [r for r in rules if r[2] and r[0] in ("macos", "wecom", "feishu")]
            if not active:
                logger.debug("notify_rules 全通道禁用，跳过: %s", event)
                return False
            allowed_channels = {r[0] for r in active}
            if level is None and active[0][1] in LEVELS:
                final_level = active[0][1]

        # R23：正文统一脱敏
        body = _redact(message or "")
        head = _redact(title or default_title)  # R23：标题同样脱敏（手机号/微信号等）
        if platform and not title:
            head = f"{head} · {platform}"

        # R16 幂等：同一 type+platform+正文 在 cron window（30min）内只发一次
        # 多篇草稿保存正文含各自标题 → 正文不同不会互相去重
        if self._is_duplicate(db_type, platform, body):
            logger.info("通知去重命中，跳过: type=%s platform=%s", db_type, platform)
            return False

        # 多通道投递
        sent_channels: list[str] = []
        for channel in ("macos", "wecom", "feishu"):
            if allowed_channels is not None and channel not in allowed_channels:
                continue
            ccfg = self.channels.get(channel)
            if ccfg is None or not ccfg.enabled:
                continue
            if channel == "macos" and not macos_available():
                continue
            try:
                if channel == "macos":
                    ok = self._send_macos(head, body)
                else:
                    ok = self._send_webhook(channel, ccfg.webhook_url_env, head, body)
            except Exception:  # noqa: BLE001 — 通知失败不能影响主流程
                logger.exception("通知通道异常 channel=%s", channel)
                ok = False
            if ok:
                sent_channels.append(channel)

        self._save(db_type, final_level, platform, head, body,
                   sent_channels[0] if sent_channels else None)

        if not sent_channels:
            logger.warning(
                "通知未投递成功（无可用通道）: type=%s level=%s %s",
                db_type, final_level, head,
            )
            return False

        logger.info("🔔 通知已发送 [%s/%s] %s · 通道=%s",
                    final_level, db_type, head, ",".join(sent_channels))
        return True

    # ------------------------------------------------------------------
    # 通道自检（R4：系统设置「发送测试通知」）
    # ------------------------------------------------------------------

    def send_test(self) -> dict:
        """逐通道发一条测试通知，**不写 notifications 表**（不污染 R16 去重窗口）。

        返回 {"macos": "ok|failed|disabled", ...}；不暴露 webhook URL（R23）。
        """
        head = "通知通道测试"
        body = f"这是一条测试通知（{datetime.now().strftime('%H:%M:%S')}），收到即配置正常。"
        results: dict[str, str] = {}
        for channel in ("macos", "wecom", "feishu"):
            ccfg = self.channels.get(channel)
            if ccfg is None or not ccfg.enabled:
                results[channel] = "disabled"
                continue
            if channel == "macos" and not macos_available():
                results[channel] = "unavailable"
                continue
            try:
                ok = (
                    self._send_macos(head, body)
                    if channel == "macos"
                    else self._send_webhook(channel, ccfg.webhook_url_env, head, body)
                )
                results[channel] = "ok" if ok else "failed"
            except Exception:  # noqa: BLE001 — 自检失败只回报状态
                logger.exception("通知自检异常 channel=%s", channel)
                results[channel] = "failed"
        return results

    # ------------------------------------------------------------------
    # 通道实现
    # ------------------------------------------------------------------

    def _send_macos(self, title: str, body: str) -> bool:
        script = (
            f'display notification "{_as_escape(body)}" '
            f'with title "{_as_escape(_APP_TITLE)}" '
            f'subtitle "{_as_escape(title)}"'
        )
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if proc.returncode != 0:
            logger.warning("macOS 通知失败: %s", _redact((proc.stderr or "").strip()))
            return False
        return True

    def _send_webhook(
        self, channel: str, url_env: Optional[str], title: str, body: str,
    ) -> bool:
        """企微 / 飞书群机器人 webhook。URL 只从 .env 读（R23，绝不写日志）。"""
        if not url_env:
            logger.debug("%s 未配置 webhook_url_env，跳过", channel)
            return False
        url = os.getenv(url_env)
        if not url:
            logger.info("%s webhook 未设置（.env 缺少 %s），跳过", channel, url_env)
            return False

        # .env 可能由人工维护；最终目标必须在实际 TCP 连接中固定为校验通过的公网 IP。
        text = f"【{title}】{body}"
        if channel == "wecom":
            payload = {"msgtype": "text", "text": {"content": text}}
        else:  # feishu
            payload = {"msg_type": "text", "content": {"text": text}}

        try:
            from ..safe_http import post_public_json
            status_code, response_body, _content_type = post_public_json(
                url, payload, timeout=5, max_bytes=1024 * 1024
            )
        except ValueError as exc:
            logger.warning("%s webhook 目标被安全校验拒绝：%s", channel, str(exc))
            return False

        if status_code >= 300:
            logger.warning("%s webhook 返回 %s（URL 已脱敏不打印）", channel, status_code)
            return False

        # 企微/飞书 200 也可能业务失败，保守看 errcode
        try:
            data = json.loads(response_body)
        except Exception:  # noqa: BLE001 — 非 JSON 响应按成功处理
            return True
        errcode = data.get("errcode", data.get("code", 0))
        if errcode not in (0, None):
            logger.warning("%s webhook 业务失败 errcode=%s msg=%s",
                           channel, errcode, data.get("errmsg") or data.get("msg"))
            return False
        return True

    # ------------------------------------------------------------------
    # 落库 + 去重
    # ------------------------------------------------------------------

    def _is_duplicate(self, db_type: str, platform: Optional[str], body: str) -> bool:
        if self.conn is None:
            return False
        cutoff = (datetime.now() - timedelta(minutes=self.dedupe_window_minutes)).isoformat(sep=" ")
        row = self.conn.execute(
            "SELECT 1 FROM notifications "
            "WHERE type = ? AND COALESCE(platform, '') = ? AND COALESCE(body, '') = ? "
            "AND sent_at >= ? LIMIT 1",
            (db_type, platform or "", body, cutoff),
        ).fetchone()
        return row is not None

    def _save(
        self, db_type: str, level: str, platform: Optional[str],
        title: str, body: str, channel: Optional[str],
    ) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(
                "INSERT INTO notifications (level, type, platform, title, body, channel, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    level, db_type, platform, title, body, channel,
                    # 带微秒：表上 UNIQUE(type, platform, sent_at) 只到秒，
                    # 同一秒内多条同类型通知（如连存多篇草稿）会撞约束丢记录
                    datetime.now().isoformat(sep=" "),
                ),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001 — 通知审计失败不能拖垮业务
            logger.exception("通知落库失败 type=%s", db_type)


# 模块级单例（便于 scheduler/executor 未注入时的兜底）
_active: Optional[Notifier] = None


def set_active(notifier: Notifier) -> None:
    global _active
    _active = notifier


def get_active() -> Optional[Notifier]:
    return _active
