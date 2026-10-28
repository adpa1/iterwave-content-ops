"""扫码登录：命令参数、状态机与 Easel 行为对齐。"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from src.core import account_login as al


@pytest.fixture()
def tmp_login(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "LOGIN_DIR", tmp_path / "_login")
    monkeypatch.setattr(al, "PROFILE_BASE", tmp_path / "_profiles")
    monkeypatch.setattr(al, "STORAGE_STATE_DIR", tmp_path / "login_states")
    monkeypatch.setattr(al, "_schedule_export", lambda platform: None)
    monkeypatch.delenv("ITERWAVE_LOGIN_HEADED", raising=False)
    al._LOGIN_PROCESSES.clear()
    al._ensure_dirs()
    yield tmp_path
    al._LOGIN_PROCESSES.clear()


def _write_status(state, qr=""):
    al._files("xhs")["status"].write_text(json.dumps({"state": state, "message": "m", "qr": qr, "ts": 1}))


def test_cmd_matches_easel_defaults(tmp_login):
    xhs = al.build_cmd("xhs")
    douyin = al.build_cmd("douyin")
    ks = al.build_cmd("kuaishou")
    assert "--no-proxy" in xhs and "--no-proxy" in douyin       # 小红书/抖音必须直连
    assert all("--headed" not in c for c in (xhs, douyin, ks))   # 默认 headless（Easel 同款）
    assert "--sms-code-file" in douyin
    assert ks[ks.index("--platform") + 1] == "kuaishou"
    assert al.build_cmd("wechat_channels")[al.build_cmd("wechat_channels").index("--platform") + 1] == "weixin-channels"


def test_headed_is_opt_in(tmp_login, monkeypatch):
    monkeypatch.setenv("ITERWAVE_LOGIN_HEADED", "1")
    assert "--headed" in al.build_cmd("xhs")


def test_profile_names_match_vendor_scripts():
    sys.path.insert(0, str(al.VENDOR_SCRIPTS))
    try:
        import douyin_publish
        import web_publisher
        import xhs_publish
    except ModuleNotFoundError as e:  # pragma: no cover — 依赖未装时跳过
        pytest.skip(str(e))
    assert al.PROFILE_NAMES["xhs"] == xhs_publish.PROFILE_NAME
    assert al.PROFILE_NAMES["douyin"] == douyin_publish.PROFILE_NAME
    assert al.PROFILE_NAMES["kuaishou"] == web_publisher.PLATFORMS["kuaishou"]["profile"]
    assert al.PROFILE_NAMES["wechat_channels"] == web_publisher.PLATFORMS["weixin-channels"]["profile"]


def test_unknown_while_process_alive_is_starting(tmp_login):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    al._LOGIN_PROCESSES["xhs"] = proc
    try:
        st = al.get_login_status("xhs")
        assert st["state"] == "starting" and st["proc_alive"] is True
    finally:
        proc.kill()


def test_process_exit_without_terminal_state_is_error(tmp_login):
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    proc.wait()
    al._LOGIN_PROCESSES["xhs"] = proc
    _write_status("qr_ready")
    st = al.get_login_status("xhs")
    assert st["state"] == "error" and "退出码 3" in st["message"]


def test_qr_only_returned_in_qr_states(tmp_login):
    al._files("xhs")["qr"].write_bytes(b"\x89PNG fake")
    _write_status("qr_ready")
    st = al.get_login_status("xhs")
    assert st["qr_base64"].startswith("data:image/png;base64,") and st["qr_ts"] > 0
    _write_status("expired")
    assert al.get_login_status("xhs")["qr_base64"] == ""


def test_logged_in_requires_success_or_storage_state(tmp_login):
    al.profile_dir("xhs").mkdir(parents=True)
    (al.profile_dir("xhs") / "Cookies").write_text("x")   # profile 里有文件 ≠ 已登录
    assert al.is_logged_in("xhs") is False
    _write_status("success")
    assert al.is_logged_in("xhs") is True
    al.clear_login("xhs")
    assert not al.profile_dir("xhs").exists()
    assert al.is_logged_in("xhs") is False


def test_sms_code_validation(tmp_login):
    with pytest.raises(ValueError):
        al.submit_sms_code("douyin", "12")
    al.submit_sms_code("douyin", " 12-34 56 ")
    assert al._files("douyin")["code"].read_text() == "123456"
