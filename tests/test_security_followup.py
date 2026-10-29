from __future__ import annotations

import os
from pathlib import Path

from src.core import config_store, env_store


def test_write_env_atomically_preserves_other_values_and_private_mode(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("EXISTING=value\nSECRET=old\n", encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setenv("ITERWAVE_ENV_FILE", str(path))

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
