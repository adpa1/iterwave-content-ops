"""Tenant SQLite repository for user-maintained active-operation targets."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from src.core.config import ActiveCommentTarget, OpsTargetsConfig

logger = logging.getLogger(__name__)
PLATFORMS = ("xhs", "douyin", "kuaishou", "wechat_channels")
SEED_MARKER = "ops_targets_yaml_seed_v1"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _lock(conn):
    lock = getattr(conn, "_lock", None)
    if lock:
        lock.acquire()
    return lock


def _unlock(lock) -> None:
    if lock:
        lock.release()


def _normalize(payload: dict[str, Any]) -> list[tuple[str, str, str, str | None, int]]:
    if not isinstance(payload, dict):
        raise ValueError("targets 必须是对象")
    unknown = set(payload) - set(PLATFORMS)
    if unknown:
        raise ValueError(f"未知平台：{', '.join(sorted(unknown))}")
    rows: list[tuple[str, str, str, str | None, int]] = []
    for platform in PLATFORMS:
        value = payload.get(platform, {})
        if not isinstance(value, dict):
            raise ValueError(f"{platform} 配置必须是对象")
        comments = value.get("active_comments", [])
        follows = value.get("follows", [])
        if not isinstance(comments, list) or len(comments) > 500:
            raise ValueError(f"{platform}.active_comments 必须是最多 500 项的数组")
        if not isinstance(follows, list) or len(follows) > 500:
            raise ValueError(f"{platform}.follows 必须是最多 500 项的数组")
        order = 0
        for item in comments:
            if not isinstance(item, dict):
                raise ValueError(f"{platform} 评论目标必须包含 url 和 content")
            url = str(item.get("url") or "").strip()
            content = str(item.get("content") or "").strip()
            try:
                parsed = urlsplit(url)
                valid_url = parsed.scheme in ("http", "https") and bool(parsed.hostname)
                has_userinfo = parsed.username is not None or parsed.password is not None
            except ValueError:
                valid_url, has_userinfo = False, True
            if not valid_url or has_userinfo or len(url) > 2048:
                raise ValueError(f"{platform} 评论目标 URL 必须是无认证信息的 http(s) 地址，且不超过 2048 字符")
            if not content or len(content) > 1000:
                raise ValueError(f"{platform} 评论内容必须为 1-1000 字")
            rows.append((platform, "active_comment", url, content, order))
            order += 1
        order = 0
        for item in follows:
            if not isinstance(item, str):
                raise ValueError(f"{platform} 关注目标必须是字符串")
            target = item.strip()
            if not target or len(target) > 512 or "\n" in target or "\r" in target:
                raise ValueError(f"{platform} 关注目标必须为 1-512 字")
            if target.startswith(("http://", "https://")):
                try:
                    parsed = urlsplit(target)
                    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"{platform} 关注目标 URL 无效或包含认证信息")
            rows.append((platform, "follow", target, None, order))
            order += 1
    return rows


def read_targets(conn) -> dict[str, dict[str, list]]:
    result = {platform: {"active_comments": [], "follows": []} for platform in PLATFORMS}
    rows = conn.execute(
        "SELECT platform, action_type, target, content FROM ops_targets "
        "ORDER BY platform, action_type, sort_order, id"
    ).fetchall()
    for row in rows:
        if row["action_type"] == "active_comment":
            result[row["platform"]]["active_comments"].append({
                "url": row["target"], "content": row["content"] or "",
            })
        else:
            result[row["platform"]]["follows"].append(row["target"])
    return result


def load_ops_targets(conn) -> dict[str, OpsTargetsConfig]:
    raw = read_targets(conn)
    return {
        platform: OpsTargetsConfig(
            active_comments=[ActiveCommentTarget(**item) for item in value["active_comments"]],
            follows=list(value["follows"]),
        )
        for platform, value in raw.items()
    }


def seed_from_yaml(conn, configured: dict[str, Any]) -> dict[str, int]:
    """One-time import from config.yaml; empty YAML still records completion."""
    marker = conn.execute(
        "SELECT 1 FROM tenant_setting_migrations WHERE migration_key=?", (SEED_MARKER,)
    ).fetchone()
    if marker:
        return {"imported": 0}
    payload: dict[str, Any] = {}
    for platform in PLATFORMS:
        item = (configured or {}).get(platform)
        if item is None:
            payload[platform] = {"active_comments": [], "follows": []}
        elif hasattr(item, "model_dump"):
            payload[platform] = item.model_dump()
        elif isinstance(item, dict):
            payload[platform] = item
        else:
            raise ValueError(f"config.yaml ops_targets.{platform} 结构无效")
    rows = _normalize(payload)
    lock = _lock(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for platform, action, target, content, order in rows:
            conn.execute(
                "INSERT INTO ops_targets(platform,action_type,target,content,sort_order,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (platform, action, target, content, order, _now(), _now()),
            )
        conn.execute(
            "INSERT INTO tenant_setting_migrations(migration_key,completed_at,imported_count,rejected_count) "
            "VALUES(?,?,?,0)",
            (SEED_MARKER, _now(), len(rows)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _unlock(lock)
    return {"imported": len(rows)}


def replace_targets(conn, payload: dict[str, Any]) -> dict[str, dict[str, list]]:
    rows = _normalize(payload)
    lock = _lock(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ops_targets")
        now = _now()
        for platform, action, target, content, order in rows:
            conn.execute(
                "INSERT INTO ops_targets(platform,action_type,target,content,sort_order,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (platform, action, target, content, order, now, now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _unlock(lock)
    return read_targets(conn)
