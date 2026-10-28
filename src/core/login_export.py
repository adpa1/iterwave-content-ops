"""把 vendor 登录脚本的持久化 profile 导出为 Playwright storage_state。

用法：python -m src.core.login_export --platform xhs --profile-base data/_profiles --out data/login_states/xhs.json

必须用与登录脚本相同的启动方式（同一浏览器内核 / profile / 启动参数），否则
macOS 钥匙串等平台上 Cookie 解密密钥不一致，导出的会是空 Cookie。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "easel"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

WEB_PUBLISHER_KEYS = {"kuaishou": "kuaishou", "wechat_channels": "weixin-channels"}


def _open_context(p, platform: str, base: str):
    if platform == "xhs":
        import xhs_publish
        return xhs_publish._launch(p, headed=False, base=base, proxy=None, login_assets=False)
    if platform == "douyin":
        import douyin_publish
        return douyin_publish._launch(p, headed=False, base=base, proxy=None)
    if platform in WEB_PUBLISHER_KEYS:
        import web_publisher
        key = WEB_PUBLISHER_KEYS[platform]
        profile = web_publisher._profile_dir(key, base)
        return p.chromium.launch_persistent_context(
            str(profile), headless=True, args=web_publisher.LAUNCH_ARGS + ["--no-proxy-server"])
    raise SystemExit(f"不支持的平台: {platform}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--profile-base", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = _open_context(p, a.platform, a.profile_base)
        try:
            state = ctx.storage_state()
        finally:
            ctx.close()
    if not state.get("cookies"):
        print("profile 中没有任何 Cookie，未导出（登录可能未真正落盘）", file=sys.stderr)
        return 2
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    print(f"exported {len(state['cookies'])} cookies → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
