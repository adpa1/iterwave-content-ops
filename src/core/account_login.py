"""账号登录启动与状态读取（直接复用 Easel publish.py login 子命令）。

流程与 Easel web/app.py api_login_start 完全等价：
  1. subprocess.Popen 调 vendor/easel/[xhs|douyin]_publish.py login / web_publisher.py login-qr
  2. 脚本自己处理 headless Playwright + login_qr.prepare_qr + login_state.write_status
  3. FastAPI 只轮询 JSON 状态文件 + 读 QR png（转 base64 给前端）

依赖：
  vendor/easel/xhs_publish.py   login --qr-out --status-file --timeout
  vendor/easel/douyin_publish.py login --qr-out --status-file --sms-code-file --timeout
  vendor/easel/web_publisher.py  login-qr --platform --qr-out --status-file --timeout
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VENDOR_SCRIPTS = PROJECT_ROOT / "src" / "vendor" / "easel"

# 登录产物目录（QR PNG + 状态 JSON + storage_state）
LOGIN_DIR = PROJECT_ROOT / "data" / "_login"
LOGIN_DIR.mkdir(parents=True, exist_ok=True)
# Easel publish.py login 持久化 profile 目录（绕过 Trae sandbox 对 ~/.easel-browser-profiles 的限制）
PROFILE_BASE = PROJECT_ROOT / "data" / "_profiles"
PROFILE_BASE.mkdir(parents=True, exist_ok=True)

# subprocess 句柄表（平台 → Popen）——前端 restart 时先 terminate
_LOGIN_PROCESSES: dict[str, subprocess.Popen] = {}
_PROCESS_LOCK = threading.Lock()

LOGIN_TIMEOUT = 240

# 平台 → (backend, 命令构造器)
# backend: xhs / douyin / web / biliup / unsupported
def _build_cmd(platform: str, qr_out: Path, status_file: Path, sms_file: Path | None = None) -> list[str]:
    """构造 subprocess 命令（与 Easel web/app.py _start_login 完全对齐）。"""
    py = sys.executable
    profile_base = str(PROFILE_BASE)
    # 默认 headed（Trae sandbox 内 headless 被小红书反爬拦截 + CloakBrowser 被拦）
    if platform == "xhs":
        return [
            py, str(VENDOR_SCRIPTS / "xhs_publish.py"), "login",
            "--profile-base", profile_base,
            "--qr-out", str(qr_out),
            "--status-file", str(status_file),
            "--timeout", str(LOGIN_TIMEOUT),
            "--headed",  # Trae sandbox 内必须 headed（headless 被反爬）
        ]
    elif platform == "douyin":
        cmd = [
            py, str(VENDOR_SCRIPTS / "douyin_publish.py"), "login",
            "--profile-base", profile_base,
            "--qr-out", str(qr_out),
            "--status-file", str(status_file),
            "--timeout", str(LOGIN_TIMEOUT),
            "--headed",
        ]
        if sms_file:
            cmd += ["--sms-code-file", str(sms_file)]
        return cmd
    elif platform == "kuaishou":
        return [
            py, str(VENDOR_SCRIPTS / "web_publisher.py"), "login-qr",
            "--platform", "kuaishou",
            "--profile-base", profile_base,
            "--qr-out", str(qr_out),
            "--status-file", str(status_file),
            "--timeout", str(LOGIN_TIMEOUT),
            "--headed",
        ]
    elif platform == "wechat_channels":
        return [
            py, str(VENDOR_SCRIPTS / "web_publisher.py"), "login-qr",
            "--platform", "weixin-channels",
            "--profile-base", profile_base,
            "--qr-out", str(qr_out),
            "--status-file", str(status_file),
            "--timeout", str(LOGIN_TIMEOUT),
            "--headed",
        ]
    else:
        raise ValueError(f"不支持的平台: {platform}")


def _platform_files(platform: str) -> tuple[Path, Path]:
    """返回 (qr_png_path, status_json_path)。"""
    return (
        LOGIN_DIR / f"{platform}.png",
        LOGIN_DIR / f"{platform}.json",
    )


def start_login(platform: str, restart: bool = False) -> dict:
    """启动后台登录，立即返回当前状态。二维码由前端轮询状态文件获取（base64）。

    返回 dict 格式（对齐 Easel web/app.py api_login_start 返回值）：
      {"state": "...", "message": "...", "qr": "base64 或空", "platform": "..."}
    """
    qr_png, status_json = _platform_files(platform)

    with _PROCESS_LOCK:
        existing = _LOGIN_PROCESSES.get(platform)
        if existing is not None and existing.poll() is None:
            if not restart:
                return get_login_status(platform)
            # restart：先 terminate 旧进程
            try:
                existing.terminate()
                try:
                    existing.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    existing.kill()
            except Exception:
                pass
            _LOGIN_PROCESSES.pop(platform, None)

        # 清理旧产物
        for f in (qr_png, status_json):
            try:
                f.unlink()
            except OSError:
                pass

        try:
            cmd = _build_cmd(platform, qr_png, status_json)
        except ValueError as e:
            return {"state": "error", "message": str(e), "qr": "", "platform": platform}

        # 抖音需要短信验证码文件（先删旧的）
        if platform == "douyin":
            sms_file = LOGIN_DIR / f"{platform}.code"
            try:
                sms_file.unlink()
            except OSError:
                pass
            cmd = _build_cmd(platform, qr_png, status_json, sms_file)

        # 起进程：cwd=PROJECT_ROOT，log 写文件（不阻塞 PIPE）
        log_file = LOGIN_DIR / f"{platform}.log"
        log_fh = open(log_file, "a", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        except Exception as e:
            log_fh.close()
            return {"state": "error", "message": f"subprocess 启动失败: {e}", "qr": "", "platform": platform}
        finally:
            log_fh.close()

        _LOGIN_PROCESSES[platform] = proc

    # 立即读状态文件（Easel 脚本已经写了 starting）
    return get_login_status(platform)


def get_login_status(platform: str) -> dict:
    """读 JSON 状态文件 + QR png（转 base64）。

    返回 dict（对齐前端 Accounts 页期望的结构）：
      {
        "state": "starting|qr_ready|scanned|success|expired|error|unknown",
        "message": "...",
        "qr": "data:image/png;base64,... 或空串",
        "platform": "xhs",
        "proc_alive": True/False,
      }
    """
    _, status_json = _platform_files(platform)
    qr_png, _ = _platform_files(platform)

    # 读 JSON 状态
    state_data = {"state": "unknown", "message": "", "qr": "", "ts": 0}
    if status_json.exists():
        try:
            state_data = json.loads(status_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # proc 存活检查
    proc = _LOGIN_PROCESSES.get(platform)
    proc_alive = proc is not None and proc.poll() is None

    # 进程已死但 JSON 还是 starting/qr_ready → runner 异常退出 → 标 error
    if not proc_alive and state_data.get("state") in ("starting", "qr_ready", "scanned"):
        # 查日志最后一行
        log_file = LOGIN_DIR / f"{platform}.log"
        last_line = ""
        if log_file.exists():
            try:
                last_line = log_file.read_text(encoding="utf-8").splitlines()[-1][:200]
            except Exception:
                pass
        state_data = {
            "state": "error",
            "message": f"登录进程异常退出。{last_line}" if last_line else "登录进程异常退出",
            "qr": "",
            "ts": int(time.time()),
        }

    # === 二维码获取：三层 fallback ===
    # Easel cmd_login 在 JSON 的 "qr" 字段存的可能是：
    #   (a) 空串 → 没二维码
    #   (b) PNG 文件路径 str (如 "data/_login/xhs.png") → headed 模式 window_login 分支
    #   (c) base64 字符串 → headless 模式下我自己的 _login_worker.py 写的
    # 我们需要最终转成纯 base64（不含 data: 前缀）

    qr_raw = state_data.get("qr", "") or ""
    qr_b64 = ""

    if qr_raw and isinstance(qr_raw, str):
        qr_raw_s = qr_raw.strip()
        # 情况 c：纯 base64 — 判断依据：不以路径分隔符结尾、不含常见路径扩展名、且长得像 base64
        is_b64_like = (
            not qr_raw_s.startswith(("/", "./", "../"))
            and not qr_raw_s.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            and len(qr_raw_s) > 100
            and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
                    for c in qr_raw_s[:64])
        )
        if is_b64_like:
            qr_b64 = qr_raw_s
        else:
            # 情况 b：把它当 PNG 文件路径来读
            candidate = Path(qr_raw_s)
            if not candidate.is_absolute():
                candidate = (PROJECT_ROOT / candidate).resolve()
            if candidate.exists() and candidate.stat().st_size > 1000:
                try:
                    qr_b64 = base64.b64encode(candidate.read_bytes()).decode("ascii")
                except Exception:
                    qr_b64 = ""

    # 最后兜底：直接读固定路径的 png 文件
    if not qr_b64 and qr_png.exists() and qr_png.stat().st_size > 1000:
        try:
            qr_b64 = base64.b64encode(qr_png.read_bytes()).decode("ascii")
        except Exception:
            qr_b64 = ""

    # 存 storage_state 的判断
    storage_dir = _profile_dir(platform)
    has_storage = storage_dir.exists() and list(storage_dir.glob("*"))

    # 统一给前端：qr_base64 = data:image/png;base64,xxx（AccountInfo 和 LoginStatus 接口期望的字段名）
    qr_data_uri = f"data:image/png;base64,{qr_b64}" if qr_b64 else ""

    return {
        "state": state_data.get("state", "unknown"),
        "message": state_data.get("message", ""),
        "qr": qr_b64,              # 纯 base64（向后兼容）
        "qr_base64": qr_data_uri,   # 完整 data URI（前端 Accounts.tsx 用这个）
        "qr_data_uri": qr_data_uri, # 别名
        "platform": platform,
        "platform_display": {"xhs": "小红书", "douyin": "抖音",
                             "kuaishou": "快手", "wechat_channels": "视频号"}.get(platform, platform),
        "proc_alive": proc_alive,
        "has_storage_state": bool(has_storage),
        "ts": state_data.get("ts", 0),
    }


def cancel_login(platform: str) -> dict:
    """终止登录进程 + 清理状态文件（用户取消登录）。"""
    with _PROCESS_LOCK:
        proc = _LOGIN_PROCESSES.pop(platform, None)
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass

    qr_png, status_json = _platform_files(platform)
    for f in (qr_png, status_json):
        try:
            f.unlink()
        except OSError:
            pass

    return {"ok": True, "platform": platform}


def clear_login(platform: str) -> dict:
    """注销登录：清 storage_state.json（下次发布前需重新扫码）。"""
    storage_dir = _profile_dir(platform)
    if storage_dir.exists():
        import shutil
        try:
            shutil.rmtree(storage_dir)
        except Exception:
            pass
    return {"ok": True, "platform": platform}


def _profile_dir(platform: str) -> Path:
    """Playwright 持久化 context profile 目录。Easel publish 脚本自己也会创建这个。"""
    return PROJECT_ROOT / "data" / "_profiles" / platform


def get_all_accounts() -> list[dict]:
    """返回四平台状态汇总（Accounts 页用）。"""
    accounts = []
    for p in ["xhs", "douyin", "kuaishou", "wechat_channels"]:
        st = get_login_status(p)
        has_storage = st.get("has_storage_state", False)
        # 登录状态判断：有 storage_state + 进程不是 starting → 算已登录
        if has_storage:
            status = "logged_in"
        elif st.get("proc_alive"):
            status = "logging_in"
        else:
            status = "not_logged_in"
        accounts.append({
            "platform": p,
            "status": status,
            "login_proc_state": st["state"],
            "login_proc_message": st["message"],
            "has_storage_state": has_storage,
        })
    return accounts
