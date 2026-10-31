"""pytest 公共夹具：临时库 + 造数工具。

所有用例都在 tmp_path 下的独立 SQLite 上跑，不碰 data/easel.db（R23 数据隔离）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest

from src.core.config import Config, load_config
from src.core.db import Database


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    """真实 config.yaml 基线 + 全部路径重定向到 tmp_path。"""
    base = load_config()
    paths = base.paths.model_copy(
        update={
            "data_dir": str(tmp_path / "data"),
            "db_file": str(tmp_path / "easel.db"),
            "backup_dir": str(tmp_path / "backups"),
            "output_dir": str(tmp_path / "data" / "outputs"),
            "screenshot_dir": str(tmp_path / "data" / "screenshots"),
            "log_dir": str(tmp_path / "data" / "logs"),
        }
    )
    # 测试数据显式设置一个画像；生产新装配置保持空白，要求用户自行保存定位。
    test_persona = base.ultimate_goal.model_copy(update={
        "persona_keywords": ["个人成长", "实用经验", "生活方式", "效率方法"],
    })
    return base.model_copy(update={"paths": paths, "ultimate_goal": test_persona})


@pytest.fixture()
def conn(cfg: Config):
    """迁移到最新 schema 的临时库连接。"""
    db = Database(cfg)
    db.initialize()
    c = db.connect()
    yield c
    c.close()


# =====================================================================
# 造数工具
# =====================================================================

class Seed:
    """最小可用的轮次/选题/内容/草稿/指标造数器。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._round_no = 0

    def topic(self, topic_id: str = "t1", dimension: str = "AI教育") -> str:
        self.conn.execute(
            "INSERT OR IGNORE INTO topic_index (id, dimension, source, title) VALUES (?, ?, 'test', ?)",
            (topic_id, dimension, topic_id),
        )
        self.conn.commit()
        return topic_id

    def round(self, status: str = "created", collecting_started_at: Optional[str] = None) -> int:
        self._round_no += 1
        cur = self.conn.execute(
            "INSERT INTO spiral_rounds (round_number, status, topic_ids, content_variants, "
            "collecting_metrics_started_at) VALUES (?, ?, '[]', '[]', ?)",
            (self._round_no, status, collecting_started_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def draft(
        self,
        round_id: int,
        platform: str = "xhs",
        draft_status: str = "published",
        topic_id: str = "t1",
        saved_at: Optional[str] = None,
        fetch_status: Optional[str] = "success",
        views: Optional[int] = None,
        likes: Optional[int] = None,
        collects: Optional[int] = None,
        comments: Optional[int] = None,
    ) -> str:
        """造一条 content + draft（+ 可选 metrics）。"""
        self.topic(topic_id)
        content_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO contents (id, round_id, topic_id, platform, title, body) "
            "VALUES (?, ?, ?, ?, 'T', 'B')",
            (content_id, round_id, topic_id, platform),
        )
        draft_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO drafts (id, content_id, platform, status, saved_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (draft_id, content_id, platform, draft_status, saved_at),
        )
        if fetch_status is not None:
            self.conn.execute(
                "INSERT INTO metrics (draft_id, content_id, platform, fetch_status, fetched_at, attempt, "
                "views, likes, collects, comments) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (draft_id, content_id, platform, fetch_status,
                 datetime.now().isoformat(sep=" "), views, likes, collects, comments),
            )
        self.conn.commit()
        return draft_id


@pytest.fixture()
def seed(conn) -> Seed:
    return Seed(conn)


@pytest.fixture()
def test_auth(monkeypatch, tmp_path):
    """API 端点测试使用隔离的访问口令，并由测试客户端登录，避免依赖开发机 .env。"""
    import os
    from src.core import auth

    monkeypatch.setenv("ITERWAVE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    auth._reset_revocations()
    auth._failures.clear()
    auth.set_password("Test-access9!")

    def authorize(client):
        response = client.post("/api/auth/login", json={"password": "Test-access9!"})
        assert response.status_code == 200
        # 租户作用域写 API 必须显式声明当前测试账号组；部分测试会注入非 default 运行时。
        from src.api import main as api_main
        tenant_id = str(api_main._g_ctx.get("runtime_tenant_id") or "default")
        client.headers.update({"X-Tenant-Id": tenant_id})
        return client

    yield authorize
    auth._reset_revocations()
    auth._failures.clear()
    os.environ.pop(auth.PASSWORD_ENV, None)


