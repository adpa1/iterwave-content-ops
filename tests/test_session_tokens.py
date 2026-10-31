"""签名会话：重启不掉线、跨进程登出、改口令失效、篡改拒绝。"""
import pytest

from src.core import auth


@pytest.fixture(autouse=True)
def isolate_auth_environment(monkeypatch, tmp_path):
    """所有口令、会话密钥和吊销表均写入本用例临时目录，不触碰真实 config/.env。"""
    monkeypatch.setenv("ITERWAVE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(auth.SESSION_SECRET_ENV, raising=False)
    auth._reset_revocations()
    auth._failures.clear()
    yield
    auth._reset_revocations()
    auth._failures.clear()


def _simulate_restart():
    auth._revoked.clear()
    auth._revoked_mtime = -1.0


def test_session_survives_restart():
    auth.set_password("Secret-123!")
    token = auth.login("Secret-123!", ip="1.1.1.1")
    _simulate_restart()
    assert auth._session_valid(token)


def test_logout_is_visible_to_other_processes():
    auth.set_password("Secret-123!")
    token = auth.login("Secret-123!", ip="1.1.1.1")
    auth.logout_session(token)
    _simulate_restart()
    assert not auth._session_valid(token)
    assert auth._revocation_path().stat().st_mode & 0o777 == 0o600


def test_password_change_invalidates_sessions():
    auth.set_password("Secret-123!")
    token = auth.login("Secret-123!", ip="1.1.1.1")
    auth.set_password("Secret-234!")
    assert not auth._session_valid(token)


def test_tampered_or_garbage_token_rejected():
    auth.set_password("Secret-123!")
    token = auth.login("Secret-123!", ip="1.1.1.1")
    exp, nonce, sig = token.split(".")
    assert not auth._session_valid(f"{int(exp) + 999}.{nonce}.{sig}")
    assert not auth._session_valid("abc")
    assert not auth._session_valid("x" * 400)


def test_no_session_without_password():
    assert not auth._session_valid("1.2.3")
