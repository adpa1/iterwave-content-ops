from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from src.core import auth, config_store, env_store, kb_sources


def test_write_env_atomically_preserves_other_values_and_private_mode(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("EXISTING=value\nSECRET=old\n", encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setenv("ITERWAVE_ENV_FILE", str(path))
    monkeypatch.setenv("SECRET", "old")

    result = env_store.write_env("SECRET", "new-secret")

    assert result == path
    assert path.read_text(encoding="utf-8") == "EXISTING=value\nSECRET=new-secret\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".env.*.tmp"))
    assert os.environ["SECRET"] == "new-secret"


def test_load_overrides_restricts_legacy_file_permissions(tmp_path, monkeypatch):
    path = tmp_path / "config_overrides.json"
    path.write_text('{"ui.theme":"dark"}', encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(config_store, "OVERRIDES_FILE", path)

    assert config_store.load_overrides(None) == {"ui.theme": "dark"}
    assert path.stat().st_mode & 0o777 == 0o600



def _request(peer: str, host: str, headers: dict[str, str] | None = None):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        url=SimpleNamespace(hostname=host),
        headers=headers or {},
    )


def test_local_bootstrap_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("ITERWAVE_ALLOW_LOCAL_BOOTSTRAP", raising=False)
    request = _request("127.0.0.1", "localhost")
    assert not auth.is_trusted_local_setup(request)

    monkeypatch.setenv("ITERWAVE_ALLOW_LOCAL_BOOTSTRAP", "1")
    assert auth.is_trusted_local_setup(request)


def test_local_bootstrap_rejects_forwarded_or_non_loopback_requests(monkeypatch):
    monkeypatch.setenv("ITERWAVE_ALLOW_LOCAL_BOOTSTRAP", "1")
    assert not auth.is_trusted_local_setup(
        _request("127.0.0.1", "localhost", {"x-forwarded-for": "203.0.113.10"})
    )
    assert not auth.is_trusted_local_setup(_request("203.0.113.10", "localhost"))
    assert not auth.is_trusted_local_setup(_request("127.0.0.1", "example.com"))



def test_write_env_rejects_invalid_keys_and_multiline_values(tmp_path, monkeypatch):
    monkeypatch.setenv("ITERWAVE_ENV_FILE", str(tmp_path / ".env"))

    for key in ("bad-key", "LOWERCASE", "A=INJECTED", "A" * 65):
        try:
            env_store.write_env(key, "value")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid key to be rejected: {key!r}")

    for value in ("line1\nINJECTED=yes", "line1\rINJECTED=yes"):
        try:
            env_store.write_env("SAFE_KEY", value)
        except ValueError:
            pass
        else:
            raise AssertionError("expected multiline value to be rejected")

    assert not (tmp_path / ".env").exists()



def test_xlsx_column_coordinate_is_bounded():
    assert kb_sources._xlsx_col_index("A1") == 0
    assert kb_sources._xlsx_col_index("SH1") < kb_sources.MAX_XLSX_COLUMNS
    try:
        kb_sources._xlsx_col_index("AAAA1")
    except ValueError:
        pass
    else:
        raise AssertionError("expected excessive Excel column coordinate to be rejected")
