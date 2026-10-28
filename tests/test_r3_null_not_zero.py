"""R3 / R15：NULL 不等于 0 —— 指标字段无数据就是 NULL，比率不做除法。"""

from __future__ import annotations

import sqlite3

from src.adapters.base import MetricsSnapshot, Platform
from src.core.metrics import Normalizer


# ---------------------------------------------------------------------
# R15：DB 层指标列禁止 DEFAULT 0
# ---------------------------------------------------------------------

METRIC_COLUMNS = (
    "views", "likes", "collects", "comments", "shares", "new_followers",
    "like_rate", "collect_rate", "interaction_rate", "follower_efficiency",
)


def test_metrics_columns_have_no_default_zero(conn: sqlite3.Connection) -> None:
    """metrics 表所有指标列默认值必须为空（NULL），不能 DEFAULT 0。"""
    bad: list[tuple[str, object]] = []
    for col in METRIC_COLUMNS:
        row = conn.execute(
            "SELECT dflt_value FROM pragma_table_info('metrics') WHERE name = ?", (col,)
        ).fetchone()
        assert row is not None, f"metrics 缺少指标列 {col}"
        if row[0] is not None:
            bad.append((col, row[0]))
    assert bad == [], f"发现 DEFAULT 值（应全为 NULL）：{bad}"


def test_insert_without_numbers_yields_all_null_metrics(seed) -> None:
    """只写 fetch_status、不写任何数字 → 指标列全为 NULL，而不是 0。"""
    r = seed.round()
    d = seed.draft(r, fetch_status="success")
    seed.conn.row_factory = sqlite3.Row
    row = seed.conn.execute("SELECT * FROM metrics WHERE draft_id = ?", (d,)).fetchone()
    assert row is not None
    for col in METRIC_COLUMNS:
        assert row[col] is None, f"{col} 应为 NULL，实际 {row[col]!r}"


# ---------------------------------------------------------------------
# R3：应用层比率 —— 分母 NULL / 0 一律 None
# ---------------------------------------------------------------------

def test_safe_ratio_returns_none_when_denominator_unknown() -> None:
    assert Normalizer.safe_ratio(100, None) is None      # views 未知
    assert Normalizer.safe_ratio(100, 0) is None         # views 为 0
    assert Normalizer.safe_ratio(None, None) is None
    assert Normalizer.safe_ratio(10, 100) == 0.1         # 正常路径
    # R3：分子未知同样不能当 0 算，否则产出假比率
    assert Normalizer.safe_ratio(None, 100) is None


def test_null_numerator_makes_rate_null() -> None:
    """R3：分子任一 NULL → 该比率 NULL（不允许 `or 0` 造出 0.0）。"""
    snap = MetricsSnapshot(
        platform=Platform.XHS,
        views=100, likes=None, collects=3, comments=1, new_followers=None,
    )
    assert snap.interaction_rate is None      # likes 缺失
    assert snap.like_rate is None             # likes 缺失
    assert snap.collect_rate == 0.03          # collects 已知 → 可算
    assert snap.follower_efficiency is None   # new_followers 缺失


def test_null_views_makes_every_rate_null() -> None:
    """views 为 NULL（未知）→ 四个比率全 NULL，绝不补 0。"""
    snap = MetricsSnapshot(
        platform=Platform.XHS,
        views=None, likes=12, collects=3, comments=1, new_followers=2,
    )
    assert snap.like_rate is None
    assert snap.collect_rate is None
    assert snap.interaction_rate is None
    assert snap.follower_efficiency is None


def test_zero_views_also_makes_every_rate_null() -> None:
    """views = 0（真实零值）同样不做除法。"""
    snap = MetricsSnapshot(
        platform=Platform.XHS,
        views=0, likes=5, collects=0, comments=0, new_followers=0,
    )
    assert snap.interaction_rate is None
    assert snap.like_rate is None


def test_zero_likes_is_not_confused_with_null() -> None:
    """likes=0（真实零互动）→ 比率是 0.0，不是 NULL。"""
    snap = MetricsSnapshot(
        platform=Platform.XHS,
        views=100, likes=0, collects=0, comments=0, new_followers=0,
    )
    assert snap.interaction_rate == 0.0
    assert snap.like_rate == 0.0


# ---------------------------------------------------------------------
# R13：z-score 平台内部基准
# ---------------------------------------------------------------------

def test_z_score_none_when_value_unknown(seed) -> None:
    r = seed.round()
    seed.draft(r, views=100, fetch_status="success")
    seed.draft(r, views=200, fetch_status="success", topic_id="t2")
    n = Normalizer(seed.conn)
    assert n.z_score(Platform.XHS, "views", None) is None


def test_z_score_is_computed_per_platform(seed) -> None:
    """同一数值在小红书/抖音各自的分布里 z 不同（R13 不合并四平台）。"""
    r = seed.round()
    for i, v in enumerate([100, 200, 300]):
        seed.draft(r, views=v, fetch_status="success", topic_id=f"tx{i}")
    for i, v in enumerate([1000, 2000, 3000]):
        seed.draft(r, platform="douyin", views=v, fetch_status="success", topic_id=f"td{i}")
    n = Normalizer(seed.conn)
    z_xhs = n.z_score(Platform.XHS, "views", 300)
    z_dy = n.z_score(Platform.DOUYIN, "views", 3000)
    assert z_xhs is not None and z_dy is not None
    # 各自都是本平台最大值 → z 应相同（同为本平台 +1σ 附近），而非按合并分布算
    assert abs(z_xhs - z_dy) < 1e-6
