from __future__ import annotations

import json
import os
import sqlite3
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

    for key in ("bad-key", "lowercase", "A=INJECTED", "A" * 65):
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

def test_local_text_source_reads_are_size_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_sources, "MAX_UPLOAD_BYTES", 8)
    path = tmp_path / "small.csv"
    path.write_bytes(b"title\na")
    assert kb_sources._read_text(path) == "title\na"

    path.write_bytes(b"title\n12345")
    try:
        kb_sources._read_text(path)
    except ValueError as exc:
        assert "大小上限" in str(exc)
    else:
        raise AssertionError("oversized local text sources must be rejected")


def test_topic_parsers_enforce_row_limits(monkeypatch):
    monkeypatch.setattr(kb_sources, "MAX_TOPIC_ROWS", 2)

    assert len(kb_sources.parse_csv("title\na\nb")[1]) == 2
    for parser, payload in (
        (kb_sources.parse_csv, "title\na\nb\nc"),
        (kb_sources.parse_json, json.dumps([{"title": "a"}, {"title": "b"}, {"title": "c"}])),
        (kb_sources.parse_jsonl, '{"title":"a"}\n{"title":"b"}\n{"title":"c"}'),
        (kb_sources.parse_rss, "<rss><channel><title>x</title><item><title>a</title></item><item><title>b</title></item><item><title>c</title></item></channel></rss>"),
    ):
        try:
            parser(payload)
        except ValueError as exc:
            assert "行上限" in str(exc)
        else:
            raise AssertionError(f"{parser.__name__} should enforce the row limit")


def test_sqlite_table_names_are_quoted_consistently(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_sources, "MAX_TOPIC_ROWS", 10)
    path = tmp_path / "topics.sqlite"
    connection = sqlite3.connect(path)
    try:
        table_name = 'odd"] OR 1=1 --'
        quoted = '"' + table_name.replace('"', '""') + '"'
        connection.execute(f"CREATE TABLE {quoted} (title TEXT)")
        connection.execute(f"INSERT INTO {quoted} VALUES ('safe title')")
        connection.commit()
    finally:
        connection.close()

    columns, rows = kb_sources.parse_sqlite(path, None)
    assert columns == ["title"]
    assert rows == [{"title": "safe title"}]


def test_registry_save_is_atomic_and_private(tmp_path):
    cfg = SimpleNamespace(paths=SimpleNamespace(data_dir=str(tmp_path)))
    source = {"id": "kb-test_1", "kind": "server", "name": "test"}

    kb_sources.save_registry(cfg, [source])

    path = kb_sources.registry_file(cfg)
    assert json.loads(path.read_text(encoding="utf-8"))["sources"] == [source]
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".sources.json.*.tmp"))


def test_load_registry_restricts_legacy_permissions(tmp_path):
    cfg = SimpleNamespace(paths=SimpleNamespace(data_dir=str(tmp_path)))
    path = kb_sources.registry_file(cfg)
    path.write_text(json.dumps({"sources": [{"id": "legacy1", "kind": "server"}]}), encoding="utf-8")
    path.chmod(0o644)

    assert kb_sources.load_registry(cfg)[0]["id"] == "legacy1"
    assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_source_id_is_reported_as_source_error(tmp_path):
    cfg = SimpleNamespace(paths=SimpleNamespace(data_dir=str(tmp_path)))
    result = kb_sources.read_source(cfg, {
        "id": "../outside",
        "kind": "remote",
        "name": "invalid source",
        "url": "https://example.com/feed.csv",
        "format": "csv",
    })
    assert result["status"] == "error"
    assert result["items"] == []


def test_source_cache_ids_cannot_escape_cache_directory(tmp_path):
    cfg = SimpleNamespace(paths=SimpleNamespace(data_dir=str(tmp_path)))
    try:
        kb_sources._source_path(cfg, {"id": "../outside", "kind": "remote", "format": "csv"})
    except ValueError as exc:
        assert "ID" in str(exc)
    else:
        raise AssertionError("path-like source IDs must be rejected")


def test_config_overrides_save_is_atomic_and_private(tmp_path, monkeypatch):
    path = tmp_path / "config_overrides.json"
    monkeypatch.setattr(config_store, "OVERRIDES_FILE", path)

    config_store._save_overrides(None, {"ui.theme": "dark"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"ui.theme": "dark"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".config_overrides.json.*.tmp"))



def test_sqlite_main_wal_shm_permissions_are_restricted(tmp_path: Path) -> None:
    import stat

    db_path = tmp_path / "private.sqlite3"
    paths = [
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
    ]
    for path in paths:
        path.write_text("private", encoding="utf-8")
        path.chmod(0o644)

    from src.core.db import _restrict_sqlite_file_permissions

    _restrict_sqlite_file_permissions(db_path)

    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_webhook_rejects_non_official_hosts_without_network(monkeypatch) -> None:
    from src.core import safe_http
    from src.core.notify import Notifier

    monkeypatch.setenv("TEST_WECOM_WEBHOOK", "https://attacker.example/hook")
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        return 200, b'{"errcode":0}', "application/json"

    monkeypatch.setattr(safe_http, "post_public_json", fake_post)
    notifier = object.__new__(Notifier)

    assert notifier._send_webhook("wecom", "TEST_WECOM_WEBHOOK", "title", "body") is False
    assert called is False


def test_webhook_allows_official_host_via_safe_transport(monkeypatch) -> None:
    from src.core import safe_http
    from src.core.notify import Notifier

    monkeypatch.setenv("TEST_FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/test")
    calls = []

    def fake_post(url, payload, **kwargs):
        calls.append((url, payload, kwargs))
        return 200, b'{"code":0}', "application/json"

    monkeypatch.setattr(safe_http, "post_public_json", fake_post)
    notifier = object.__new__(Notifier)

    assert notifier._send_webhook("feishu", "TEST_FEISHU_WEBHOOK", "title", "body") is True
    assert len(calls) == 1
    assert calls[0][0].startswith("https://open.feishu.cn/")
