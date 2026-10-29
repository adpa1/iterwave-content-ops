"""账号组完整快照备份与停机恢复。

备份包含 SQLite 一致性快照、数据库登记的图片素材文件和账号组运行配置覆盖。
不包含登录态 Cookie、API 密钥或 shared/ 公共原始采集数据。
恢复必须在应用停止后执行；恢复前自动保留数据库与素材目录的回滚副本。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class BackupError(ValueError):
    """备份文件无效或恢复未能安全完成。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(db_path: Path | str, data_dir: Path | str,
                  backup_dir: Path | str, tenant_id: str) -> Path:
    db_path, data_dir, backup_dir = Path(db_path), Path(data_dir), Path(backup_dir)
    if not db_path.is_file():
        raise BackupError("数据库文件不存在，无法创建快照")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = backup_dir / f"iterwave_{tenant_id}_{ts}.zip"
    media_dir = data_dir / "media"
    overrides = data_dir / "config_overrides.json"

    with tempfile.TemporaryDirectory(prefix="iterwave-backup-") as temp_name:
        temp_dir = Path(temp_name)
        snapshot_db = temp_dir / "easel.db"
        source = sqlite3.connect(str(db_path), timeout=30)
        target = sqlite3.connect(str(snapshot_db))
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise BackupError("SQLite 快照完整性检查失败")
        finally:
            target.close()
            source.close()

        media_files: dict[str, dict[str, object]] = {}
        if media_dir.is_dir():
            try:
                snapshot = sqlite3.connect(str(snapshot_db))
                names = {
                    row[0] for row in snapshot.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='media_assets'"
                    )
                }
                if names:
                    stored = snapshot.execute(
                        "SELECT stored_filename FROM media_assets WHERE status='active'"
                    ).fetchall()
                else:
                    stored = []
            finally:
                snapshot.close()
            for (filename,) in stored:
                name = Path(str(filename)).name
                source_file = (media_dir / name).resolve()
                if (
                    Path(name).suffix.lower() not in ALLOWED_IMAGE_SUFFIXES
                    or not source_file.is_relative_to(media_dir.resolve())
                    or not source_file.is_file()
                ):
                    continue
                size = source_file.stat().st_size
                if size > MAX_BACKUP_BYTES:
                    raise BackupError(f"素材文件超过备份上限：{name}")
                media_files[name] = {
                    "sha256": _sha256(source_file),
                    "size": size,
                }

        manifest = {
            "version": 1,
            "tenant_id": tenant_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "database_sha256": _sha256(snapshot_db),
            "media_files": media_files,
            "includes_config_overrides": overrides.is_file(),
            "excludes": ["login states and browser cookies", "API keys", "shared raw feeds"],
        }
        try:
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot_db, "easel.db")
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for name in media_files:
                    archive.write(media_dir / name, f"media/{name}")
                if overrides.is_file():
                    archive.write(overrides, "config_overrides.json")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    backups = sorted(backup_dir.glob(f"iterwave_{tenant_id}_*.zip"), reverse=True)
    for old in backups[10:]:
        old.unlink(missing_ok=True)
    return destination


def restore_backup(backup_path: Path | str, db_path: Path | str, data_dir: Path | str,
                   backup_dir: Path | str, tenant_id: str) -> dict[str, str]:
    """安全恢复账号组快照。调用方必须确保应用与调度器已经停止。"""
    backup_path, db_path, data_dir, backup_dir = (
        Path(backup_path).resolve(), Path(db_path).resolve(),
        Path(data_dir).resolve(), Path(backup_dir).resolve(),
    )
    if not backup_path.is_relative_to(backup_dir) or not backup_path.is_file():
        raise BackupError("只能恢复当前账号组备份目录中的文件")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    media_dir = data_dir / "media"
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(prefix="iterwave-restore-", dir=backup_dir) as temp_name:
        stage = Path(temp_name)
        try:
            with zipfile.ZipFile(backup_path, "r") as archive:
                infos = archive.infolist()
                if sum(info.file_size for info in infos) > MAX_BACKUP_BYTES:
                    raise BackupError("备份解压后超过 2GB 上限")
                allowed = {"manifest.json", "easel.db", "config_overrides.json"}
                for info in infos:
                    name = PurePosixPath(info.filename)
                    if name.is_absolute() or ".." in name.parts or info.is_dir():
                        raise BackupError("备份包含非法路径")
                    if info.filename not in allowed and not (
                        len(name.parts) == 2 and name.parts[0] == "media"
                        and name.suffix.lower() in ALLOWED_IMAGE_SUFFIXES
                    ):
                        raise BackupError(f"备份包含不允许的文件：{info.filename}")
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("version") != 1 or manifest.get("tenant_id") != tenant_id:
                    raise BackupError("备份版本或账号组标识不匹配")
                db_bytes = archive.read("easel.db")
                db_stage = stage / "easel.db"
                db_stage.write_bytes(db_bytes)
                if _sha256(db_stage) != manifest.get("database_sha256"):
                    raise BackupError("备份数据库校验失败")
                check_conn = sqlite3.connect(str(db_stage))
                try:
                    check = check_conn.execute("PRAGMA integrity_check").fetchone()
                    if not check or check[0] != "ok":
                        raise BackupError("备份数据库完整性检查失败")
                finally:
                    check_conn.close()

                stage_media = stage / "media"
                stage_media.mkdir()
                expected = manifest.get("media_files") or {}
                for filename, metadata in expected.items():
                    safe_name = Path(filename).name
                    if safe_name != filename or Path(safe_name).suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
                        raise BackupError("备份清单包含非法素材文件名")
                    archive_name = f"media/{safe_name}"
                    raw = archive.read(archive_name)
                    if len(raw) != int(metadata.get("size", -1)):
                        raise BackupError(f"素材大小校验失败：{safe_name}")
                    file_stage = stage_media / safe_name
                    file_stage.write_bytes(raw)
                    if _sha256(file_stage) != metadata.get("sha256"):
                        raise BackupError(f"素材校验失败：{safe_name}")
                config_stage = stage / "config_overrides.json"
                if "config_overrides.json" in archive.namelist():
                    config_stage.write_bytes(archive.read("config_overrides.json"))
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as exc:
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"备份读取失败：{exc}") from exc

        # 先保留回滚副本；数据库 WAL/SHM 在停机恢复前一并移走。
        db_rollback = backup_dir / f"pre_restore_{tenant_id}_{ts}.db"
        if db_path.exists():
            shutil.copy2(db_path, db_rollback)
        for suffix in ("-wal", "-shm"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        media_rollback = backup_dir / f"pre_restore_media_{tenant_id}_{ts}"
        if media_dir.exists():
            os.replace(media_dir, media_rollback)
        try:
            os.replace(stage / "easel.db", db_path)
            os.replace(stage / "media", media_dir)
            config_path = data_dir / "config_overrides.json"
            if (stage / "config_overrides.json").exists():
                os.replace(stage / "config_overrides.json", config_path)
            else:
                config_path.unlink(missing_ok=True)
        except Exception as exc:
            # 尽最大努力回滚目录；保留回滚副本供人工恢复数据库。
            if media_dir.exists():
                shutil.rmtree(media_dir, ignore_errors=True)
            if media_rollback.exists():
                os.replace(media_rollback, media_dir)
            if db_rollback.exists():
                shutil.copy2(db_rollback, db_path)
            raise BackupError(f"恢复未完成，已尝试回滚：{exc}") from exc
    return {"database": str(db_path), "media_dir": str(media_dir), "rollback_db": str(db_rollback)}
