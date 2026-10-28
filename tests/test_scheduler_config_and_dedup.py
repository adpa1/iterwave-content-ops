"""定时任务可配置 + 热点去重 / 保留。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.core.config import DEFAULT_JOB_SCHEDULES, JobScheduleConfig, SchedulerConfig
from src.core.config_store import ConfigPatchError, changed_job_ids, patch_config
from src.core.hot_topic_dedup import mark_cross_platform, normalize_title, prune, similarity, topic_id
from src.core.hot_topics import HotTopicCollector
from src.core.scheduler import JOB_RUNNERS, SchedulerEngine


# ---------------- 定时任务配置 ----------------

def test_every_job_comes_from_config():
    assert set(JOB_RUNNERS) == set(DEFAULT_JOB_SCHEDULES)
    sc = SchedulerConfig(jobs={"hot_topics_refresh": {"interval_minutes": 240}})
    assert sc.job("hot_topics_refresh").interval_minutes == 240
    assert sc.job("daily_ops").cron == "0 8 * * *"          # 未配置的用默认值


@pytest.mark.parametrize("bad", [
    {"interval_minutes": 10, "cron": "0 8 * * *"},   # 两个都配
    {},                                             # 都没配
    {"cron": "every day"},                          # 非法 cron
    {"interval_minutes": 0},
])
def test_job_schedule_validation(bad):
    with pytest.raises(ValueError):
        JobScheduleConfig(**bad)


def test_unknown_job_rejected():
    with pytest.raises(ValueError):
        SchedulerConfig(jobs={"nope": {"interval_minutes": 5}})


def test_register_jobs_respects_enabled_and_trigger(cfg, tmp_path):
    cfg.scheduler.jobs["kb_scan"] = JobScheduleConfig(enabled=False, cron="0 6 * * *")
    cfg.scheduler.jobs["hot_topics_refresh"] = JobScheduleConfig(interval_minutes=180)
    eng = SchedulerEngine(cfg, tmp_path / "jobs.db")
    eng._register_all_jobs()
    ids = {j.id for j in eng.scheduler.get_jobs()}
    assert "kb_scan" not in ids and "hot_topics_refresh" in ids
    assert "3:00:00" in str(eng.scheduler.get_job("hot_topics_refresh").trigger)
    # 运行时改成 cron 并关闭/开启
    cfg.scheduler.jobs["hot_topics_refresh"] = JobScheduleConfig(cron="0 */4 * * *")
    eng.apply_job_config("hot_topics_refresh")
    assert "cron" in str(eng.scheduler.get_job("hot_topics_refresh").trigger)


def test_patch_switches_trigger_and_persists(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.config_store.OVERRIDES_FILE", tmp_path / "ov.json")
    r = patch_config(cfg, conn, {"scheduler.jobs.daily_ops.interval_minutes": 720})
    job = cfg.scheduler.job("daily_ops")
    assert job.interval_minutes == 720 and job.cron is None
    assert changed_job_ids(c["key"] for c in r["changed"]) == {"daily_ops"}
    patch_config(cfg, conn, {"scheduler.jobs.daily_ops.cron": "30 9 * * 1-5"})
    job = cfg.scheduler.job("daily_ops")
    assert job.cron == "30 9 * * 1-5" and job.interval_minutes is None
    import json
    ov = json.loads((tmp_path / "ov.json").read_text())
    assert "scheduler.jobs.daily_ops.interval_minutes" not in ov       # 互斥键已清理
    with pytest.raises(ConfigPatchError):
        patch_config(cfg, conn, {"scheduler.jobs.daily_ops.cron": "bad"})
    with pytest.raises(ConfigPatchError):
        patch_config(cfg, conn, {"scheduler.jobs.hot_topics_refresh.interval_minutes": 1})


# ---------------- 去重 ----------------

def test_normalize_and_id_rules():
    assert normalize_title("ＡＩ  教育，火了！🔥") == normalize_title("ai教育火了")
    assert topic_id("weibo", "AI 教育火了！") == topic_id("weibo", "ai教育火了")
    assert topic_id("weibo", "AI教育火了") != topic_id("douyin", "AI教育火了")
    assert similarity("苹果发布新款手机", "苹果发布新款手机了") > 0.8


def test_repeated_collection_updates_instead_of_inserting(cfg, conn, monkeypatch):
    c = HotTopicCollector(cfg, conn)
    batch = [{"id": topic_id("weibo", t), "title": t, "raw_url": "https://x", "heat_score": h,
              "heat_unit": "热度", "time_sensitive": None}
             for t, h in (("AI 教育火了！", 100), ("ai教育火了", 120), ("另一条", 50))]
    for _ in range(5):                       # 模拟 5 次定时采集
        c._upsert("weibo", batch)
    rows = conn.execute("SELECT title, seen_count FROM hot_topics ORDER BY title").fetchall()
    assert len(rows) == 2                    # 不随采集次数增长
    assert {r["seen_count"] for r in rows} == {5}


def test_cross_platform_marks_duplicates(cfg, conn):
    now = datetime.now()
    for pid, plat, title, first in (("a", "weibo", "苹果发布新款手机", now - timedelta(hours=2)),
                                     ("b", "douyin", "苹果发布新款手机了", now - timedelta(hours=1)),
                                     ("c", "zhihu", "完全不同的话题", now)):
        conn.execute(
            "INSERT INTO hot_topics (id, source, discovered_at, last_seen_at, evidence_status, title, "
            "platform, title_norm, heat_score, time_sensitive) VALUES (?,?,?,?, 'collected', ?, ?, ?, 1e6, NULL)",
            (pid, f"60s.viki.moe/{plat}", first.isoformat(timespec="seconds"),
             now.isoformat(timespec="seconds"), title, plat, normalize_title(title)))
    conn.commit()
    assert mark_cross_platform(conn, threshold=0.8, window_hours=48) == 1
    dup = dict(conn.execute("SELECT id, duplicate_of FROM hot_topics").fetchall())
    assert dup == {"a": None, "b": "a", "c": None}      # 最早发现的为主条目
    items = HotTopicCollector(cfg, conn).overview()["items"]
    a = next(i for i in items if i["id"] == "a")
    assert [x["platform"] for x in a["also_on"]] == ["douyin"]
    assert "b" not in {i["id"] for i in items}


def test_prune_by_days_and_cap(conn):
    now = datetime.now()
    for i in range(10):
        ts = (now - timedelta(days=i, minutes=1 if i else 0)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO hot_topics (id, source, discovered_at, last_seen_at, evidence_status, title) "
                     "VALUES (?, 's/weibo', ?, ?, 'collected', ?)", (f"t{i}", ts, ts, f"t{i}"))
    conn.commit()
    assert prune(conn, retention_days=7, max_rows=1000) == 3        # 7、8、9 天前
    assert prune(conn, retention_days=7, max_rows=4) == 3           # 剩 7 条 → 上限 4
    left = {r[0] for r in conn.execute("SELECT id FROM hot_topics")}
    assert left == {"t0", "t1", "t2", "t3"}
