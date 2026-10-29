from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.core.tenant_backup import create_backup, restore_backup


def test_tenant_backup_restores_database_media_and_overrides(tmp_path: Path) -> None:
    data_dir = tmp_path / "tenants" / "brand_a" / "data"
    backup_dir = tmp_path / "tenants" / "brand_a" / "backups"
    media_dir = data_dir / "media"
    output_dir = data_dir / "outputs"
    data_dir.mkdir(parents=True)
    media_dir.mkdir()
    output_dir.mkdir()
    backup_dir.mkdir(parents=True)
    db_path = data_dir / "easel.db"

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE media_assets (
            id INTEGER PRIMARY KEY,
            stored_filename TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE state (value TEXT NOT NULL);
        CREATE TABLE contents (id TEXT PRIMARY KEY, image_paths TEXT);
        INSERT INTO state VALUES ('before');
        INSERT INTO media_assets VALUES (1, 'image_a.png', 'active');
    """)
    conn.close()
    (media_dir / "image_a.png").write_bytes(b"image-original")
    card_path = output_dir / "card_a.png"
    card_path.write_bytes(b"card-original")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO contents VALUES (?, ?)", ("content-a", json.dumps([str(card_path)])))
    conn.commit()
    conn.close()
    (data_dir / "config_overrides.json").write_text('{"quota": 10}', encoding="utf-8")

    archive = create_backup(db_path, data_dir, backup_dir, "brand_a")

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE state SET value='after'")
    conn.commit()
    conn.close()
    (media_dir / "image_a.png").write_bytes(b"image-changed")
    card_path.write_bytes(b"card-changed")
    (data_dir / "config_overrides.json").write_text('{"quota": 99}', encoding="utf-8")

    result = restore_backup(archive, db_path, data_dir, backup_dir, "brand_a")

    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT value FROM state").fetchone()[0] == "before"
    conn.close()
    assert (media_dir / "image_a.png").read_bytes() == b"image-original"
    assert card_path.read_bytes() == b"card-original"
    assert json.loads((data_dir / "config_overrides.json").read_text(encoding="utf-8")) == {"quota": 10}
    assert Path(result["rollback_db"]).is_file()


def test_restore_rejects_duplicate_zip_members(tmp_path: Path) -> None:
    import zipfile
    import pytest
    from src.core.tenant_backup import BackupError

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    archive_path = backup_dir / "iterwave_brand_a_duplicate.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("manifest.json", "{}")
    with pytest.raises(BackupError, match="重复文件条目"):
        restore_backup(archive_path, tmp_path / "easel.db", tmp_path / "data", backup_dir, "brand_a")


def test_restore_rejects_non_object_manifest(tmp_path: Path) -> None:
    import zipfile
    import pytest
    from src.core.tenant_backup import BackupError

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    archive_path = backup_dir / "iterwave_brand_a_bad_manifest.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", "[]")
        archive.writestr("easel.db", b"not a database")
    with pytest.raises(BackupError, match="清单格式无效"):
        restore_backup(archive_path, tmp_path / "easel.db", tmp_path / "data", backup_dir, "brand_a")
