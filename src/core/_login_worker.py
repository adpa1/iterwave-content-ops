#!/usr/bin/env python3
"""登录 Worker（与 Easel web_publisher.py login 分支等价）。

sync Playwright + headless（默认）+ vendor login_qr.prepare_qr + login_state.write_status
起 subprocess 后，FastAPI 只 JSON 状态文件 + QR png → 前端轮询展示。

参数（argparse）：
  --platform        平台代号 (xhs / douyin / kuaishou / wechat_channels)
  --login-url       登录页 URL
  --qr-selectors    QR DOM 选择器（逗号分隔）
  --switch-selectors 切换到扫码登录 tab 的按钮选择器
  --state-path      JSON 状态文件路径（原子写）
  --qr-path         QR PNG 截图路径
  --storage-path    Playwright storage_state.json 路径
  --profile-dir     persistent context profile 目录（复用已有登录态）
  --headed          加这个参数才弹浏览器（调试用，默认 headless）
  --timeout         登录总超时秒数（默认 240）
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import sys
import time
from pathlib import Path

# sys.path：让 vendor/ 和 src/ 都能被 import
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from vendor.easel.login_state import write_status, read_status, STATES  # noqa: E402
from vendor.easel import login_qr  # noqa: E402   # prepare_qr / capture_qr / save_qr
try:
    from vendor.easel import login_transport  # noqa: E402
    _TRANSPORT_OK = True
except Exception:
    _TRANSPORT_OK = False  # 非必须，缺 requests 时降级跳过


def _install_signal_handlers(state_path: str) -> None:
    def _graceful(signum, frame):
        try:
            write_status(state_path, "expired", f"收到信号 {signum}，退出")
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)


def _save_storage(context, storage_path: str) -> None:
    """保存 storage_state.json + chmod 600（R23）。"""
    try:
        state = context.storage_state()
        target = Path(storage_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    except Exception as exc:
        sys.stderr.write(f"[_save_storage] 失败: {exc}\n")


def _write_qr_base64(state_path: str, qr_path: Path) -> None:
    """把 QR PNG base64 写进 JSON 状态文件（前端轮询直接 data:image/png;base64 渲染）。"""
    try:
        b64 = base64.b64encode(qr_path.read_bytes()).decode("ascii")
        write_status(state_path, "qr_ready", "✅ 二维码已就绪，请用手机扫码", qr=b64)
    except Exception as exc:
        write_status(state_path, "error", f"QR base64 写入失败: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--login-url", required=True)
    ap.add_argument("--qr-selectors", required=True)
    ap.add_argument("--switch-selectors", default="")
    ap.add_argument("--state-path", required=True)
    ap.add_argument("--qr-path", required=True)
    ap.add_argument("--storage-path", required=True)
    ap.add_argument("--profile-dir", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    state_path = args.state_path
    qr_path = Path(args.qr_path)
    _install_signal_handlers(state_path)

    from playwright.sync_api import sync_playwright

    write_status(state_path, "starting", f"启动 {args.platform} 登录 Worker...")

    with sync_playwright() as pw:
        headless = not args.headed
        try:
            if args.profile_dir:
                context = pw.chromium.launch_persistent_context(
                    args.profile_dir,
                    headless=headless,
                    viewport={"width": 1440, "height": 900},
                    args=["--no-proxy-server", "--disable-blink-features=AutomationControlled"],
                )
            else:
                browser = pw.chromium.launch(
                    headless=headless,
                    args=["--no-proxy-server", "--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                )
        except Exception as exc:
            write_status(state_path, "error", f"浏览器启动失败: {exc}")
            return 1

        # 公共资源代理（微信 res.wx.qq.com / 小红书 fe-static 等，headless 下绕过 CDN 拦截）
        if _TRANSPORT_OK:
            try:
                login_transport.install(context, platform=args.platform.replace("_", "-"))
            except Exception:
                pass  # transport 可选，失败不阻断

        page = context.pages[0] if context.pages else context.new_page()

        # 1. 打开登录页
        try:
            page.goto(args.login_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
        except Exception as exc:
            write_status(state_path, "error", f"打开登录页失败: {exc}")
            try:
                context.close()
            except Exception:
                pass
            return 1

        # 2. 用 Easel vendor login_qr.prepare_qr 截图 QR（sync 版本，原生适配）
        try:
            if args.switch_selectors:
                login_qr.prepare_qr(
                    page, qr_path,
                    selectors=args.qr_selectors,
                    switch_selectors=args.switch_selectors,
                    minimum_size=120,
                )
            else:
                login_qr.capture_qr(
                    page, qr_path,
                    selectors=args.qr_selectors,
                    minimum_size=120,
                )
            if not qr_path.exists() or qr_path.stat().st_size < 1000:
                raise RuntimeError("QR 文件为空或不存在")
            _write_qr_base64(state_path, qr_path)
        except Exception as exc:
            write_status(state_path, "error", f"二维码识别失败: {exc}")
            try:
                context.close()
            except Exception:
                pass
            return 1

        # 3. 轮询等扫码：检测「离开登录页 / QR 消失 / 跳转成功 URL」
        deadline = time.time() + args.timeout
        write_status(state_path, "qr_ready", "✅ 二维码已就绪，请用手机扫码", qr=(
            base64.b64encode(qr_path.read_bytes()).decode("ascii") if qr_path.exists() else ""
        ))

        scanned = False
        while time.time() < deadline:
            try:
                current_url = page.url
                # 判据 1：URL 变了（登录成功后通常会跳转）
                if current_url != args.login_url and "login" not in current_url.lower():
                    scanned = True
                    write_status(state_path, "scanned", "✅ 已扫码，页面已跳转")
                    break
                # 判据 2：尝试找 QR 元素，找不到说明扫码成功（平台页面会把 QR 替换成"扫码成功"提示）
                try:
                    locator = page.locator(args.qr_selectors.split(",")[0]).first
                    if locator.count() == 0 or not locator.is_visible(timeout=1500):
                        scanned = True
                        write_status(state_path, "scanned", "✅ 已扫码（QR 消失）")
                        break
                except Exception:
                    scanned = True
                    write_status(state_path, "scanned", "✅ 已扫码（QR 选择器异常消失）")
                    break
            except Exception:
                # 页面 crash 也可能是登录成功跳转 → 给它一次机会
                pass

            # 检查是否超时了
            time.sleep(1.5)
            if (time.time() - deadline) > 0:
                break

        # 4. 扫码后等跳转完成 + 保存 storage_state
        if scanned:
            try:
                page.wait_for_timeout(3000)  # 等跳转稳定
                # 有些平台扫码后还需要"确认登录"按钮
                for confirm_sel in [
                    "button:has-text('确认')", "button:has-text('确定')",
                    "button:has-text('同意')", "button:has-text('确认登录')",
                ]:
                    try:
                        loc = page.locator(confirm_sel).first
                        if loc.count() > 0 and loc.is_visible(timeout=1500):
                            loc.click(timeout=2000)
                            page.wait_for_timeout(1500)
                            break
                    except Exception:
                        continue

                _save_storage(context, args.storage_path)
                write_status(state_path, "success", "✅ 登录成功，storage_state 已保存")
            except Exception as exc:
                write_status(state_path, "error", f"扫码后保存失败: {exc}")
                try:
                    _save_storage(context, args.storage_path)
                except Exception:
                    pass
        else:
            write_status(state_path, "expired", "⏰ 二维码已过期，请刷新后重试")

        try:
            context.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
