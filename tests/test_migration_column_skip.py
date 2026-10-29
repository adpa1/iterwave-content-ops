"""旧库升级：已执行过部分 ALTER 的库重跑 029/030 时不得因多行语句残留而报语法错误。"""
import sqlite3
from pathlib import Path

from src.core.db import Database

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def _sql(name: str) -> str:
    return next(MIGRATIONS.glob(f"*_{name}.sql")).read_text(encoding="utf-8")


def test_content_review_rejection_rerun_on_db_that_already_has_columns():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE contents (id TEXT PRIMARY KEY, review_rejected INTEGER NOT NULL DEFAULT 0 "
                 "CHECK (review_rejected IN (0, 1)), review_rejection_reason TEXT)")
    conn.executescript(Database._skip_existing_columns(conn, _sql("content_review_rejection")))
    cols = [row[1] for row in conn.execute("PRAGMA table_info(contents)")]
    assert cols.count("review_rejected") == 1


def test_content_review_rejection_partial_state_adds_only_missing_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE contents (id TEXT PRIMARY KEY, review_rejected INTEGER NOT NULL DEFAULT 0)")
    conn.executescript(Database._skip_existing_columns(conn, _sql("content_review_rejection")))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(contents)")}
    assert {"review_rejected", "review_rejection_reason"} <= cols


def test_content_review_rejection_fresh_db_keeps_check_constraint():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE contents (id TEXT PRIMARY KEY)")
    conn.executescript(Database._skip_existing_columns(conn, _sql("content_review_rejection")))
    conn.execute("INSERT INTO contents (id) VALUES ('a')")
    try:
        conn.execute("UPDATE contents SET review_rejected = 5")
        raise AssertionError("CHECK 约束应生效")
    except sqlite3.IntegrityError:
        pass


def test_hot_topic_trend_rerun_keeps_update_statement():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE hot_topics (id INTEGER PRIMARY KEY, heat_score REAL, heat_prev REAL, heat_peak REAL)")
    conn.execute("INSERT INTO hot_topics (heat_score) VALUES (42)")
    conn.executescript(Database._skip_existing_columns(conn, _sql("hot_topic_trend")))
    assert conn.execute("SELECT heat_peak FROM hot_topics").fetchone()[0] == 42
