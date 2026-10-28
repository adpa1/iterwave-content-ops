"""
Easel 复制代码（vendor）——两个独立项目，不依赖外部目录。

这些 .py 文件从 Easel（Apache 2.0）复制而来，原样保留上游实现与文件头，
放在本仓库内随项目分发。用途：

- platform_readback.py  → 发布后读回对账（抖音/快手/B站）
- account_stats.py      → 创作者中心账号统计（小红书 _scrape_notes）
- login_state.py        → 登录状态 JSON 文件协议
- login_qr.py           → 二维码 DOM 渲染

约定（AGENTS.md R4）：
- 只复制、不引用 /Users/longyi/project/Easel/Easel-main 的运行时路径，
  因此把 Easel 目录移走/删除不影响本项目。
- 上游代码保持原样；需要改行为时在我们自己的 src/adapters/ 里改，不改 vendor。
- 升级上游 = 重新复制覆盖这几个文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

# vendor 目录本身（这些 Easel 脚本是平铺 import，需要加入 sys.path）
VENDOR_DIR = Path(__file__).resolve().parent

# 复制的文件清单（升级时对照）
VENDORED_FILES = (
    "platform_readback.py",
    "account_stats.py",
    "login_state.py",
    "login_qr.py",
)


def ensure_vendor_path() -> None:
    """把 vendor 目录加进 sys.path（仅本仓库内的目录，无外部依赖）。"""
    path = str(VENDOR_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
