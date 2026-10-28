"""全局运营软暂停开关（app_settings.ops_paused）。

与 R6 熔断的区别：
- 熔断：平台侧出现验证码/限流/登录失效时**自动**触发，按平台生效，需 3 步恢复
- 软暂停：用户主动点「暂停运营」，**全局**生效，随时可恢复，不写 circuit_breaker_events

只影响运营动作（评论回复 / 主动评论 / 关注 / 每日运营计划），
不影响生产线的存草稿与数据采集（R1 存草稿仍按用户指令执行）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

_KEY = "ops_paused"


def is_ops_paused(conn) -> bool:
    """读开关。表未迁移（老库）时按 False 处理，避免启动即中断运营。"""
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_KEY,)
        ).fetchone()
    except Exception:  # noqa: BLE001 — app_settings 不存在时降级
        return False
    return bool(row) and str(row[0]).lower() in ("1", "true", "yes", "on")


def set_ops_paused(conn, paused: bool, by: str = "web-ui") -> dict:
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (_KEY, "1" if paused else "0",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), by),
    )
    conn.commit()
    return {"paused": paused}


def pause_state(conn) -> Optional[dict]:
    """返回 {paused, updated_at, updated_by}；表不存在时返回 None。"""
    try:
        row = conn.execute(
            "SELECT value, updated_at, updated_by FROM app_settings WHERE key = ?",
            (_KEY,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return {"paused": False, "updated_at": None, "updated_by": None}
    return {
        "paused": str(row[0]).lower() in ("1", "true", "yes", "on"),
        "updated_at": row[1],
        "updated_by": row[2],
    }
