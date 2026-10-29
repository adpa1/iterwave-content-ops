"""
R23 脱敏工具 —— 独立模块避免循环依赖。

任何进日志/异常栈/ops_logs 的文本都应先走 redact()，
覆盖 cookie/token + 手机号 + 微信号三类敏感值（前 4 位 + ***）。
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_HOME = Path.home()

# cookie / token / api_key 类
_SECRET_PATTERN = re.compile(
    r"(?i)\b(cookie|token|authorization|api[_-]?key)\b\s*[:=]\s*([^\s,;]+)"
)
# 手机号（11 位 1[3-9]...）
_PHONE_PATTERN = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
# 微信号
_WX_PATTERN = re.compile(
    r"(?i)(微信|wx|vx|wechat)\s*[:：]?\s*([a-z0-9_-]{6,})"
)


def redact(text: str) -> str:
    """R23 统一脱敏：cookie/token + 手机号 + 微信号。

    规则统一：所有敏感值 **保留前 4 位 + *****。
    不完整值（≤ 4 位）直接替换为 ***，避免漏出完整值。
    """
    if not text:
        return text

    def _repl_secret(m: re.Match) -> str:
        key, value = m.group(1), m.group(2)
        if len(value) <= 4:
            return f"{key}=***"
        return f"{key}={value[:4]}***"
    text = _SECRET_PATTERN.sub(_repl_secret, text)

    def _repl_phone(m: re.Match) -> str:
        num = m.group(0)
        return f"{num[:4]}***"
    text = _PHONE_PATTERN.sub(_repl_phone, text)

    def _repl_wx(m: re.Match) -> str:
        prefix, value = m.group(1), m.group(2)
        masked = "***" if len(value) <= 4 else f"{value[:4]}***"
        return f"{prefix}: {masked}"
    text = _WX_PATTERN.sub(_repl_wx, text)

    return text


def redact_path(path: str | Path | None) -> str:
    """R23 路径脱敏：接口与页面不再暴露服务器绝对路径。

    - 项目内路径 → 相对路径（data/kb/topics.csv）
    - 用户目录内 → ~/…/文件名
    - 其它绝对路径 → …/文件名
    - Windows 盘符 / 相对路径原样返回
    """
    if not path:
        return ""
    raw = str(path)
    if not raw:
        return ""
    p = Path(raw)
    if not p.is_absolute():
        return raw
    try:
        return str(p.relative_to(_PROJECT_ROOT))
    except ValueError:
        pass
    try:
        p.relative_to(_HOME)
    except ValueError:
        return f"…/{p.name}"
    parent = p.parent.name
    return f"~/…/{parent}/{p.name}" if parent else f"~/…/{p.name}"

