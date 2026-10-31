"""账号扫码登录（复用 Easel vendor 登录脚本，行为对齐 Easel web/app.py）。

流程与 Easel `_start_login` / `_login_status` / `_account_logged_in` 一致：
  1. subprocess.Popen 调 vendor/easel/[xhs|douyin]_publish.py login / web_publisher.py login-qr
  2. 脚本自己处理 Playwright + login_qr.prepare_qr + login_state.write_status
  3. 后端只读 JSON 状态文件 + 二维码 PNG（转 base64 给前端）

与 Easel 的差异只保留两处，且都是本项目必须的：
  - --profile-base 指向 data/_profiles（项目内持久化，便于部署与备份）；
  - 登录成功后把持久化 profile 导出成 Playwright storage_state（data/login_states/<平台>.json），
    因为本项目的草稿保存 / 运营动作 / /api/status 都通过 login_manager.has_login_state 读这个文件。

此前与 Easel 行为不一致、导致「代码一样效果不一样」的点（已修正）：
  1. 强制 --headed：脚本进入 window_login 分支，要求在服务器弹出的浏览器窗口里扫码，
     抠图失败时根本不出二维码；Easel 默认 headless。现默认 headless，ITERWAVE_LOGIN_HEADED=1 才有头。
  2. 小红书 / 抖音缺 --no-proxy：环境里有 http(s)_proxy 时脚本会走代理，小红书判风险直接拦截。
  3. 启动后状态文件尚未写出时返回 unknown，前端把 unknown 当终态停止轮询 → 二维码永远不显示。
     现与 Easel 一致：进程活着时 unknown 视为 starting；进程已退出且没写终态 → error。
  4. 「是否已登录」按 data/_profiles/<平台> 目录是否存在判断，但脚本实际写的是
     data/_profiles/XiaohongshuProfile 等目录，导致登录成功也永远显示未登录；注销也删错目录。
  5. 登录成功后没有任何东西写 data/login_states，系统其余部分永远认为未登录。
  6. 二维码不区分状态返回、且无时间戳：过期 / 成功后仍显示旧码。现仅 qr_ready / window_login 返回，并带 qr_ts。
  7. 缺短信验证码回填接口：抖音风控进入 sms_required 后无法继续。现提供 submit_sms_code。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .redact import redact

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VENDOR_SCRIPTS = PROJECT_ROOT / "src" / "vendor" / "easel"

# 登录产物目录（QR PNG + 状态 JSON + 日志 + 短信验证码文件）
LOGIN_DIR = PROJECT_ROOT / "data" / "_login"
# vendor 脚本的持久化 profile 根目录（--profile-base）
PROFILE_BASE = PROJECT_ROOT / "data" / "_profiles"
# 项目其余模块读取的 storage_state（login_manager.LOGIN_DIR）
STORAGE_STATE_DIR = PROJECT_ROOT / "data" / "login_states"


def configure_data_dir(data_dir: Path | str) -> None:
    """按当前账号组配置登录产物路径；默认组仍映射原有 data/ 文件位置。"""
    global LOGIN_DIR, PROFILE_BASE, STORAGE_STATE_DIR
    root = Path(data_dir).resolve()
    LOGIN_DIR = root / "_login"
    PROFILE_BASE = root / "_profiles"
    STORAGE_STATE_DIR = root / "login_states"


PLATFORMS = ("xhs", "douyin", "kuaishou", "wechat_channels")

# 平台 → vendor 脚本里的 profile 目录名（xhs_publish.PROFILE_NAME / douyin_publish.PROFILE_NAME /
# web_publisher.PLATFORMS[*]["profile"]），必须一致，否则判断登录态、注销都会落到错误目录
PROFILE_NAMES = {
    "xhs": "XiaohongshuProfile",
    "douyin": "DouyinProfile",
    "kuaishou": "KuaishouProfile",
    "wechat_channels": "ChannelsProfile",
}
# 本项目平台代号 → web_publisher 平台代号
WEB_PUBLISHER_KEYS = {"kuaishou": "kuaishou", "wechat_channels": "weixin-channels"}
DISPLAY = {"xhs": "小红书", "douyin": "抖音", "kuaishou": "快手", "wechat_channels": "视频号"}

TERMINAL_STATES = ("success", "expired", "error")
QR_STATES = ("qr_ready", "window_login")

_LOGIN_PROCESSES: dict[object, subprocess.Popen] = {}
_PROCESS_LOCK = threading.Lock()
_EXPORT_LOCK = threading.Lock()
_EXPORTING: set[object] = set()


def _data_paths(data_dir: Path | str | None = None) -> tuple[Path, Path, Path]:
    """返回指定账号组的登录目录；不修改模块级路径，兼容旧调用者。"""
    if data_dir is None:
        return LOGIN_DIR, PROFILE_BASE, STORAGE_STATE_DIR
    root = Path(data_dir).resolve()
    return root / "_login", root / "_profiles", root / "login_states"


def _process_key(platform: str, data_dir: Path | str | None = None):
    if data_dir is None:
        return platform
    return (str(Path(data_dir).resolve()), platform)


def _ensure_dirs(data_dir: Path | str | None = None) -> None:
    # 浏览器 profile 包含可复用的社交平台 Cookie；目录权限必须阻止同机其他用户遍历。
    login_dir, profile_base, storage_state_dir = _data_paths(data_dir)
    for d in (login_dir, profile_base, storage_state_dir):
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:
            logger.warning("无法收紧登录数据目录权限：%s", d.name)


def _headed() -> bool:
    """Easel 默认 headless；本机有桌面、需要在窗口里扫码时设 ITERWAVE_LOGIN_HEADED=1。"""
    return os.environ.get("ITERWAVE_LOGIN_HEADED", "").strip().lower() in ("1", "true", "yes")


def _files(platform: str, data_dir: Path | str | None = None) -> dict[str, Path]:
    login_dir, _, storage_state_dir = _data_paths(data_dir)
    return {
        "qr": login_dir / f"{platform}.png",
        "status": login_dir / f"{platform}.json",
        "log": login_dir / f"{platform}.log",
        "code": login_dir / f"{platform}.code",
        "storage": storage_state_dir / f"{platform}.json",
    }


def profile_dir(platform: str, data_dir: Path | str | None = None) -> Path:
    _, profile_base, _ = _data_paths(data_dir)
    return profile_base / PROFILE_NAMES[platform]


def _login_env() -> dict[str, str]:
    """对齐 Easel _proxy_env：国内平台域名一律直连（no_proxy），不影响外网代理。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.setdefault(
        "no_proxy",
        "localhost,127.0.0.1,10.*,*.xiaohongshu.com,*.douyin.com,*.kuaishou.com,"
        "*.weixin.qq.com,*.qq.com",
    )
    return env


def build_cmd(platform: str, timeout_seconds: int | None = None, data_dir: Path | str | None = None) -> list[str]:
    """构造登录命令；等待时限来自当前账号组运行配置。"""
    if timeout_seconds is None:
        from .config import get_config
        timeout_seconds = get_config().timeouts.login_timeout_seconds
    f = _files(platform, data_dir)
    _, profile_base, _ = _data_paths(data_dir)
    py = sys.executable
    common = ["--profile-base", str(profile_base),
              "--qr-out", str(f["qr"]), "--status-file", str(f["status"]),
              "--timeout", str(timeout_seconds)]
    if platform == "xhs":
        cmd = [py, str(VENDOR_SCRIPTS / "xhs_publish.py"), "login", "--no-proxy", *common]
    elif platform == "douyin":
        cmd = [py, str(VENDOR_SCRIPTS / "douyin_publish.py"), "login", "--no-proxy", *common,
               "--sms-code-file", str(f["code"])]
    elif platform in WEB_PUBLISHER_KEYS:
        cmd = [py, str(VENDOR_SCRIPTS / "web_publisher.py"), "login-qr",
               "--platform", WEB_PUBLISHER_KEYS[platform], *common]
    else:
        raise ValueError(f"不支持的平台: {platform}")
    if _headed():
        cmd.append("--headed")
    return cmd


# ---------------------------------------------------------------------------
# 启动 / 取消 / 注销
# ---------------------------------------------------------------------------

def start_login(platform: str, restart: bool = False, data_dir: Path | str | None = None) -> dict:
    """启动后台登录并立即返回当前状态；二维码由前端轮询 get_login_status 获取。"""
    _ensure_dirs(data_dir)
    f = _files(platform, data_dir)
    process_key = _process_key(platform, data_dir)
    with _PROCESS_LOCK:
        existing = _LOGIN_PROCESSES.get(process_key)
        if existing is not None and existing.poll() is None:
            if not restart:
                return get_login_status(platform, data_dir=data_dir)
            existing.terminate()
            try:
                existing.wait(timeout=8)
            except subprocess.TimeoutExpired:
                existing.kill()
                existing.wait(timeout=5)
            _LOGIN_PROCESSES.pop(process_key, None)

        for key in ("qr", "status", "code"):
            f[key].unlink(missing_ok=True)

        try:
            cmd = build_cmd(platform, data_dir=data_dir)
        except ValueError as e:
            return {**_empty_status(platform), "state": "error", "message": str(e)}

        log_fh = f["log"].open("a", encoding="utf-8")
        try:
            proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=_login_env(),
                                    stdout=log_fh, stderr=subprocess.STDOUT, close_fds=True)
        except Exception as e:  # noqa: BLE001
            return {**_empty_status(platform), "state": "error", "message": f"登录进程启动失败: {e}"}
        finally:
            log_fh.close()
        _LOGIN_PROCESSES[process_key] = proc

    status = get_login_status(platform, data_dir=data_dir)
    if status["state"] == "unknown":
        status.update(state="starting", message="正在启动浏览器并准备二维码…")
    return status


def cancel_login(platform: str, data_dir: Path | str | None = None) -> dict:
    """终止登录进程 + 清理临时产物（不动已有登录态）。"""
    with _PROCESS_LOCK:
        proc = _LOGIN_PROCESSES.pop(_process_key(platform, data_dir), None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    f = _files(platform, data_dir)
    for key in ("qr", "status", "code"):
        f[key].unlink(missing_ok=True)
    return {"ok": True, "platform": platform}


def stop_tenant_logins(data_dir: Path | str, timeout_seconds: float = 10.0) -> None:
    """仅停止指定账号组的扫码进程；其他账号组的进程与导出线程不受影响。"""
    root = str(Path(data_dir).resolve())
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    with _PROCESS_LOCK:
        active = [
            (key, platform, proc)
            for key, proc in _LOGIN_PROCESSES.items()
            if isinstance(key, tuple) and key[0] == root
            for platform in (key[1],)
        ]
        completed = {(key, platform): proc.poll() is not None for key, platform, proc in active}
        for _, platform, proc in active:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    logger.warning("终止账号组 %s 平台 %s 登录子进程失败", root, platform)
        for key, platform, proc in active:
            try:
                proc.wait(timeout=max(0.1, min(3.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    raise TimeoutError(f"账号组 {root} 平台 {platform} 登录子进程未能退出")
            _LOGIN_PROCESSES.pop(key, None)

    for key, platform, proc in active:
        if completed.get((key, platform)) and _read_status_file(_files(platform, data_dir)["status"]).get("state") == "success":
            _schedule_export(platform, data_dir=data_dir)

    tenant_keys = {key for key, _, _ in active}
    while True:
        with _EXPORT_LOCK:
            exporting = bool(_EXPORTING.intersection(tenant_keys))
        if not exporting:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"账号组 {root} 登录态导出仍在执行")
        time.sleep(0.05)

    for _, platform, _ in active:
        f = _files(platform, data_dir)
        for key in ("qr", "status", "code"):
            f[key].unlink(missing_ok=True)


def stop_all_logins(timeout_seconds: float = 10.0) -> None:
    """兼容旧调用：只停止默认目录下的登录进程，不跨账号组清理。"""
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    with _PROCESS_LOCK:
        active = [(platform, proc) for key, proc in _LOGIN_PROCESSES.items()
                  if isinstance(key, str) for platform in (key,)]
        completed_before_stop = {platform: proc.poll() is not None for platform, proc in active}
        for _, proc in active:
            if proc.poll() is None:
                proc.terminate()
        for platform, proc in active:
            try:
                proc.wait(timeout=max(0.1, min(3.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            _LOGIN_PROCESSES.pop(platform, None)
    for platform, proc in active:
        if completed_before_stop.get(platform) and _read_status_file(_files(platform)["status"]).get("state") == "success":
            _schedule_export(platform)


def clear_login(platform: str, data_dir: Path | str | None = None) -> dict:
    """注销：删持久化 profile、storage_state 和登录标记（下次需重新扫码）。"""
    cancel_login(platform, data_dir=data_dir)
    shutil.rmtree(profile_dir(platform, data_dir), ignore_errors=True)
    _files(platform, data_dir)["storage"].unlink(missing_ok=True)
    return {"ok": True, "platform": platform}


def submit_sms_code(platform: str, code: str, data_dir: Path | str | None = None) -> dict:
    """回填短信验证码（对齐 Easel /api/login/{platform}/sms）：写一次性验证码文件，runner 读走后提交。"""
    digits = "".join(ch for ch in (code or "") if ch.isdigit())
    if not 4 <= len(digits) <= 8:
        raise ValueError("验证码应为 4-8 位数字")
    _ensure_dirs(data_dir)
    _files(platform, data_dir)["code"].write_text(digits, encoding="utf-8")
    return {"ok": True, "platform": platform}


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def _empty_status(platform: str) -> dict:
    return {
        "state": "unknown", "message": "", "qr": "", "qr_base64": "", "qr_data_uri": "",
        "qr_ts": 0, "platform": platform, "platform_display": DISPLAY.get(platform, platform),
        "proc_alive": False, "has_storage_state": False, "ts": 0,
    }


def _read_status_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _last_log_line(path: Path) -> str:
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return lines[-1][:200] if lines else ""
    except OSError:
        return ""


def get_login_status(platform: str, data_dir: Path | str | None = None) -> dict:
    """读状态文件 + 二维码（仅 qr_ready / window_login 时返回二维码，附 qr_ts 防缓存）。"""
    f = _files(platform, data_dir)
    raw = _read_status_file(f["status"])
    state = raw.get("state", "unknown")
    message = redact(str(raw.get("message") or ""))

    proc = _LOGIN_PROCESSES.get(_process_key(platform, data_dir))
    proc_alive = proc is not None and proc.poll() is None
    if proc is None and state not in TERMINAL_STATES and state != "unknown":
        # 账号组切换或服务重启后，状态文件可能残留，但进程句柄已不存在；不得继续显示扫码中。
        state, message = "error", "登录流程已中断，请重新发起扫码登录"
    elif proc is not None and not proc_alive and state not in TERMINAL_STATES:
        # 与 Easel 一致：runner 没写终态就退出 → 明确的 error，而不是无限转圈的 unknown
        tail = redact(_last_log_line(f["log"]))
        state, message = "error", f"登录程序异常退出（退出码 {proc.returncode}）" + (f"：{tail}" if tail else "")
    elif proc_alive and state == "unknown":
        state, message = "starting", "正在启动浏览器并准备二维码…"

    qr_b64, qr_ts = "", 0
    if state in QR_STATES:
        qr_path = f["qr"]
        ref = raw.get("qr") or ""
        if ref and not Path(ref).is_absolute():
            ref_path = (PROJECT_ROOT / ref).resolve()
            if ref_path.is_file():
                qr_path = ref_path
        elif ref and Path(ref).is_file():
            qr_path = Path(ref)
        if qr_path.is_file() and qr_path.stat().st_size > 0:
            try:
                qr_b64 = base64.b64encode(qr_path.read_bytes()).decode("ascii")
                qr_ts = qr_path.stat().st_mtime_ns // 1_000_000
            except OSError:
                qr_b64 = ""

    if state == "success" and not proc_alive:
        _schedule_export(platform)

    data_uri = f"data:image/png;base64,{qr_b64}" if qr_b64 else ""
    return {
        **_empty_status(platform),
        "state": state,
        "message": message,
        "qr": qr_b64,
        "qr_base64": data_uri,
        "qr_data_uri": data_uri,
        "qr_ts": qr_ts,
        "proc_alive": proc_alive,
        "has_storage_state": f["storage"].is_file(),
        "ts": raw.get("ts", 0),
    }


def is_logged_in(platform: str, data_dir: Path | str | None = None) -> bool:
    """对齐 Easel _account_logged_in：profile 目录里总有 Cookies 文件，存在 ≠ 已登录，
    只信「最近一次登录流程成功」或已导出的 storage_state。"""
    f = _files(platform, data_dir)
    return _read_status_file(f["status"]).get("state") == "success" or f["storage"].is_file()


def get_all_accounts(data_dir: Path | str | None = None) -> list[dict]:
    accounts = []
    for p in PLATFORMS:
        st = get_login_status(p, data_dir=data_dir)
        if is_logged_in(p, data_dir=data_dir):
            status = "logged_in"
        elif st["proc_alive"]:
            status = "logging_in"
        else:
            status = "not_logged_in"
        accounts.append({
            "platform": p,
            "platform_display": DISPLAY[p],
            "status": status,
            "login_proc_state": st["state"],
            "login_proc_message": st["message"],
            "has_storage_state": st["has_storage_state"],
        })
    return accounts


# ---------------------------------------------------------------------------
# 登录成功 → 导出 storage_state
# ---------------------------------------------------------------------------

def _schedule_export(platform: str, data_dir: Path | str | None = None) -> None:
    """登录 runner 退出后（profile 锁已释放）在后台把 profile 导出为 storage_state。"""
    f = _files(platform, data_dir)
    process_key = _process_key(platform, data_dir)
    status_mtime = f["status"].stat().st_mtime if f["status"].is_file() else 0
    if f["storage"].is_file() and f["storage"].stat().st_mtime >= status_mtime:
        return
    with _EXPORT_LOCK:
        if process_key in _EXPORTING:
            return
        _EXPORTING.add(process_key)

    def _run():
        try:
            export_storage_state(platform, data_dir=data_dir)
        finally:
            with _EXPORT_LOCK:
                _EXPORTING.discard(process_key)

    tenant_tag = Path(data_dir).resolve().name if data_dir is not None else "default"
    threading.Thread(target=_run, name=f"login-export-{tenant_tag}-{platform}", daemon=True).start()


def export_storage_state(platform: str, timeout: int | None = None, data_dir: Path | str | None = None) -> bool:
    """用与登录脚本相同的浏览器 / profile 打开一次，导出 cookies + localStorage。"""
    if timeout is None:
        from .config import get_config
        timeout = get_config().timeouts.login_export_timeout_seconds
    _ensure_dirs(data_dir)
    _, profile_base, _ = _data_paths(data_dir)
    out = _files(platform, data_dir)["storage"]
    cmd = [sys.executable, "-m", "src.core.login_export",
           "--platform", platform, "--profile-base", str(profile_base), "--out", str(out)]
    try:
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=_login_env(),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("导出 %s 登录态超时", platform)
        return False
    if r.returncode != 0:
        logger.warning("导出 %s 登录态失败：%s", platform, redact((r.stderr or r.stdout)[-300:]))
        return False
    logger.info("✅ %s 登录态已导出到 %s", platform, out)
    return True
