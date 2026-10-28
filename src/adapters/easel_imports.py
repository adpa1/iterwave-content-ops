"""
Easel 模块导入辅助 —— 从本仓库复制的代码导入（R4）。

**两个项目独立**：Easel 的代码是「复制进本仓库」的 vendor 副本
（见 src/vendor/easel/），不是运行时调用外部项目的能力。
把 /Users/longyi/project/Easel 移走或删除，本项目仍能正常运行。

升级上游的方式：重新从 Easel 的 skills/shared/scripts 复制同名文件覆盖
src/vendor/easel/ 下的副本，然后跑一次 import 自检。

可直接 import 的模块：
- login_state.py       → 登录状态 JSON 文件协议
- login_qr.py          → 二维码 DOM 渲染
- platform_readback.py → 发布后读回对账
- account_stats.py     → 创作者中心账号统计
（web_publisher.py 暂不复制：我们只实现其 draft 分支，见 src/adapters/draft_saver.py）
"""

from __future__ import annotations

from typing import Any

from ..vendor.easel import VENDOR_DIR, VENDORED_FILES, ensure_vendor_path

# 兼容旧调用名：指向本仓库内的复制目录（不再是 Easel 源码路径）
EASEL_VENDOR_DIR = VENDOR_DIR


def ensure_easel_path() -> None:
    """把 vendor 目录加入 sys.path（这些脚本是平铺 import 的独立 .py）。"""
    ensure_vendor_path()


def import_login_state():
    """导入 vendor 化的 login_state.py。"""
    ensure_easel_path()
    try:
        import login_state  # type: ignore
        return login_state
    except ImportError:
        return None


def import_platform_readback():
    """导入 vendor 化的 platform_readback.py。"""
    ensure_easel_path()
    try:
        import platform_readback  # type: ignore
        return platform_readback
    except ImportError:
        return None


def import_account_stats():
    """导入 vendor 化的 account_stats.py。"""
    ensure_easel_path()
    try:
        import account_stats  # type: ignore
        return account_stats
    except ImportError:
        return None


def check_easel_available() -> dict[str, Any]:
    """检查复制代码是否齐全、可导入。"""
    return {
        "vendor_dir": str(VENDOR_DIR),
        "vendor_dir_exists": VENDOR_DIR.exists(),
        "files_present": {
            name: (VENDOR_DIR / name).exists() for name in VENDORED_FILES
        },
        "login_state": import_login_state() is not None,
        "platform_readback": import_platform_readback() is not None,
        "account_stats": import_account_stats() is not None,
    }
