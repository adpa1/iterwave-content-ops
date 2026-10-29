"""
Agent 编排（设计方案 §21）— 步骤定义、依赖校验、持久化，以及 8 点流程诊断。

执行器（RoundExecutor）按这里保存的编排运行：
- image_card 关闭 → 不出图（此时 save_draft 必须同时关闭：图文平台存草稿需要图片）
- save_draft 关闭 → 只产出内容，不进创作者中心（干跑）
- auto_schedule 开启 → 存草稿后按母计划的平台发布时段自动排期（进内容日历）
- image_card 与 quality_gate 的先后可调（默认先质检再出图，省掉被拦内容的出图成本）

锁定步骤（不能关）：选题评估、文案生成、质量门（R2 终极目标检查不可绕过）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STEP_DEFS: dict[str, dict] = {
    "topic_eval": {
        "label": "① 选题评估", "skill": "topic_scoring", "locked": True, "requires": [],
        "desc": "从选题池读选题，按三模式评分 + 复盘反馈加减分 + 母计划维度配额选出本轮选题",
        "inputs": ["选题池（本地选题库 + 热点转入 + 手动录入）", "母计划", "上一轮复盘结论"],
    },
    "copywriting": {
        "label": "② 文案生成", "skill": "copywriter", "locked": True, "requires": ["topic_eval"],
        "desc": "同一选题按平台风格生成变体（LLM，失败降级模板）",
        "inputs": ["选题", "平台风格", "复盘文案要点"],
    },
    "quality_gate": {
        "label": "③ 质量门检查", "skill": "ultimate_goal_check", "locked": True, "requires": ["copywriting"],
        "desc": "R2 终极目标检查：红线 / 禁止方向 / 维度偏离，不通过打回重写，最多 2 次",
        "inputs": ["文案"],
    },
    "image_card": {
        "label": "④ 图片卡片", "skill": "image_card", "locked": False, "requires": ["copywriting"],
        "desc": "按平台尺寸生成知识卡片（Pillow）",
        "inputs": ["文案标题", "平台尺寸"],
    },
    "save_draft": {
        "label": "⑤ 平台存草稿", "skill": "draft_saver", "locked": False,
        "requires": ["quality_gate", "image_card"],
        "desc": "Playwright 打开创作者中心只点「存草稿」（R1 永不点发布）",
        "inputs": ["通过质检的文案", "卡片图片", "平台登录态"],
    },
    "auto_schedule": {
        "label": "⑥ 自动排期", "skill": "scheduler", "locked": False, "requires": ["save_draft"],
        "desc": "按母计划里各平台的发布时段，把草稿排进内容日历",
        "inputs": ["已存草稿", "母计划平台发布时段"],
    },
    "await_publish": {
        "label": "⑦ 等待发布确认", "skill": "publish_polling", "locked": False, "requires": ["save_draft"],
        "desc": "用户在平台发布后手动确认，或发布轮询自动发现；之后进入数据采集与复盘",
        "inputs": ["已存草稿"],
    },
}

DEFAULT_ORDER = ["topic_eval", "copywriting", "quality_gate", "image_card",
                 "save_draft", "auto_schedule", "await_publish"]
DEFAULT_ENABLED = {"auto_schedule": False}


def default_steps() -> list[dict]:
    return [{"id": s, "enabled": DEFAULT_ENABLED.get(s, True)} for s in DEFAULT_ORDER]


def validate_steps(steps: list[dict]) -> list[str]:
    """返回错误列表（空 = 合法）。规则：步骤齐全且不重复；锁定步骤必须开启；
    开启的步骤其依赖必须开启且排在它前面。"""
    errors: list[str] = []
    ids = [s.get("id") for s in steps]
    unknown = [i for i in ids if i not in STEP_DEFS]
    if unknown:
        errors.append(f"未知步骤：{unknown}")
    if len(set(ids)) != len(ids):
        errors.append("步骤重复")
    missing = [i for i in STEP_DEFS if i not in ids]
    if missing:
        errors.append(f"缺少步骤：{missing}")
    if errors:
        return errors
    pos = {s["id"]: i for i, s in enumerate(steps)}
    enabled = {s["id"]: bool(s.get("enabled")) for s in steps}
    for sid, d in STEP_DEFS.items():
        if d["locked"] and not enabled[sid]:
            errors.append(f"「{d['label']}」是锁定步骤，不能关闭")
        if not enabled[sid]:
            continue
        for dep in d["requires"]:
            if not enabled[dep]:
                errors.append(f"开启「{d['label']}」需要先开启「{STEP_DEFS[dep]['label']}」"
                              f"（缺少它的输出，无法执行）")
            elif pos[dep] > pos[sid]:
                errors.append(f"「{d['label']}」必须排在「{STEP_DEFS[dep]['label']}」之后")
    return errors


def load_steps(conn) -> tuple[list[dict], Optional[str]]:
    try:
        row = conn.execute("SELECT steps, updated_at FROM orchestration_settings WHERE id = 1").fetchone()
    except Exception:  # noqa: BLE001 — 表不存在（旧库）→ 默认编排
        row = None
    if row:
        try:
            steps = json.loads(row[0])
            if not validate_steps(steps):
                return steps, row[1]
            logger.warning("已保存的编排不合法，回退默认编排")
        except (TypeError, ValueError):
            pass
    return default_steps(), None


def save_steps(conn, steps: list[dict], author: str = "web-ui") -> dict:
    clean = [{"id": s.get("id"), "enabled": bool(s.get("enabled"))} for s in steps]
    errors = validate_steps(clean)
    if errors:
        raise ValueError("；".join(errors))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO orchestration_settings (id, steps, updated_at, updated_by) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET steps = excluded.steps, updated_at = excluded.updated_at, "
        "updated_by = excluded.updated_by",
        (json.dumps(clean), now, author),
    )
    conn.commit()
    return {"steps": clean, "updated_at": now}


class Orchestration:
    """执行器读取用的只读视图。"""

    def __init__(self, steps: list[dict]):
        self.steps = steps
        self.enabled = {s["id"]: bool(s["enabled"]) for s in steps}
        self.pos = {s["id"]: i for i, s in enumerate(steps)}

    @classmethod
    def load(cls, conn) -> "Orchestration":
        return cls(load_steps(conn)[0])

    def on(self, step: str) -> bool:
        return self.enabled.get(step, True)

    @property
    def card_before_gate(self) -> bool:
        return self.pos.get("image_card", 99) < self.pos.get("quality_gate", 0)


def view(conn) -> dict:
    steps, updated_at = load_steps(conn)
    return {
        "steps": [{**s, **{k: v for k, v in STEP_DEFS[s["id"]].items()}} for s in steps],
        "updated_at": updated_at,
        "default": default_steps(),
    }


def step_status_from_report(report: Optional[dict], orch: Orchestration) -> dict[str, dict]:
    """把一轮执行报告折算成每个编排步骤的状态（success / failed / skipped / pending）。"""
    if not report:
        return {}
    out: dict[str, dict] = {}
    steps = {s.get("step"): s for s in report.get("steps", [])}
    final = report.get("final_status")
    failed_hard = final == "failed"

    def put(sid, status, detail=""):
        out[sid] = {"status": status, "detail": detail}

    sel = steps.get("score")
    put("topic_eval", "success" if sel and sel.get("selected") else ("failed" if failed_hard else "pending"),
        f"选出 {len(sel.get('selected', []))} 个选题" if sel else "；".join(report.get("errors", [])[:1]))
    produced = report.get("contents_created", 0)
    put("copywriting", "success" if produced else ("failed" if sel else "pending"), f"产出 {produced} 篇")
    put("quality_gate", "success" if produced else ("failed" if sel else "pending"),
        f"拦截重写 {report.get('guard_rejected', 0)} 次")
    put("image_card", "skipped" if not orch.on("image_card") else ("success" if produced else "pending"),
        "" if orch.on("image_card") else "编排中已关闭")
    if not orch.on("save_draft") or steps.get("dry_run"):
        put("save_draft", "skipped", "编排中已关闭（干跑）" if not orch.on("save_draft") else "本次未存草稿")
    else:
        saved, failed = report.get("drafts_saved", 0), report.get("drafts_failed", 0)
        put("save_draft", "success" if saved else ("failed" if failed else "pending"),
            f"成功 {saved} · 失败 {failed}")
    sch = steps.get("auto_schedule")
    put("auto_schedule", "skipped" if not orch.on("auto_schedule") else ("success" if sch else "pending"),
        f"排期 {sch.get('scheduled', 0)} 篇" if sch else ("编排中已关闭" if not orch.on("auto_schedule") else ""))
    put("await_publish", "pending" if report.get("drafts_saved") else "skipped", "")
    return out


# =====================================================================
# 8 点流程诊断（§21.3）
# =====================================================================

def _item(no, name, status, message, fix_page=None, fix_tab=None):
    return {"no": no, "name": name, "status": status, "message": message,
            "fix_page": fix_page, "fix_tab": fix_tab}


def diagnose(cfg, conn, *, cb=None, live_llm: bool = False) -> dict:
    from ..adapters.base import Platform
    from .kb_sources import read_all_for_check
    from .login_manager import has_login_state

    items: list[dict] = []
    enabled = cfg.platforms.enabled_keys()

    # 1 选题库来源（真实读一遍：上传 / 远程 / 服务器路径）
    kb_check = read_all_for_check(cfg)
    kb_status = kb_check["status"]
    items.append(_item(
        1, "选题库来源可读", kb_status, kb_check["message"], "assets", "mapping"))

    # 2 账号画像
    ug = cfg.ultimate_goal
    goals = conn.execute("SELECT COUNT(*) FROM goals WHERE status='active'").fetchone()[0]
    from .persona import effective_persona
    no_persona = [p for p in enabled if not effective_persona(cfg, p)["persona_keywords"]]
    if not ug.persona_keywords and (not enabled or no_persona):
        detail = ("各平台都没有画像关键词" if len(no_persona) == len(enabled)
                  else f"{'、'.join(no_persona)} 没有画像关键词")
        items.append(_item(2, "账号画像完整", "block", f"{detail}：无法判断选题是否符合账号定位",
                           "goals", "persona"))
    elif not ug.redline_keywords and not ug.forbidden_directions:
        items.append(_item(2, "账号画像完整", "warn", "未配置红线词 / 禁止方向，质量门只能做维度检查",
                           "goals", "persona"))
    elif not goals:
        items.append(_item(2, "账号画像完整", "warn", "画像已配置，但没有进行中的目标", "goals"))
    else:
        mode_label = {"unified": "统一画像", "per_platform": "分平台画像", "hybrid": "总目标 + 分平台画像"}.get(
            ug.goal_mode, ug.goal_mode)
        items.append(_item(2, "账号画像完整", "pass", f"{mode_label} · 目标 {goals} 个"))

    # 3 登录态
    no_login = [p for p in enabled if not has_login_state(Platform(p))]
    if not enabled:
        items.append(_item(3, "平台登录态有效", "block", "没有启用任何平台", "settings", "platforms"))
    elif no_login:
        items.append(_item(3, "平台登录态有效", "block",
                           f"未登录：{', '.join(Platform(p).display_name for p in no_login)}", "accounts"))
    else:
        items.append(_item(3, "平台登录态有效", "pass", f"{len(enabled)} 个平台已登录"))

    # 4 LLM
    llm = cfg.llm
    if not llm.enabled:
        items.append(_item(4, "LLM API 连通", "warn", "LLM 未启用：文案走模板降级，复盘要点不会进文案",
                           "settings", "model"))
    elif not os.getenv(llm.api_key_env):
        items.append(_item(4, "LLM API 连通", "block", f"缺少环境变量 {llm.api_key_env}", "settings", "model"))
    elif live_llm:
        try:
            import httpx
            r = httpx.post(
                f"{llm.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {os.getenv(llm.api_key_env)}"},
                json={"model": llm.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout=min(15, llm.timeout_seconds),
            )
            r.raise_for_status()
            items.append(_item(4, "LLM API 连通", "pass", f"{llm.model} 调用成功"))
        except Exception as exc:  # noqa: BLE001
            items.append(_item(4, "LLM API 连通", "block", f"调用失败：{str(exc)[:120]}", "settings", "model"))
    else:
        items.append(_item(4, "LLM API 连通", "pass", f"已配置 {llm.model}（勾选「实测 LLM」可真调一次）"))

    # 5 熔断
    broken = []
    if cb is not None:
        for p in enabled:
            try:
                if cb.is_circuit_broken(Platform(p)):
                    broken.append(Platform(p).display_name)
            except Exception:  # noqa: BLE001
                pass
    items.append(_item(5, "熔断器全部解除", "block" if broken else "pass",
                       f"熔断中：{', '.join(broken)}" if broken else "无熔断", "operations" if broken else None,
                       "circuit" if broken else None))

    # 6 配额
    today_rows = conn.execute(
        "SELECT platform, action_type, COUNT(*) FROM ops_logs "
        "WHERE status='success' AND date(executed_at, 'localtime') = date('now','localtime') "
        "GROUP BY platform, action_type"
    ).fetchall() if _has_column(conn, "ops_logs", "executed_at") else []
    used = {(r[0], r[1]): r[2] for r in today_rows}
    exhausted = []
    for p in enabled:
        q = cfg.rhythm.daily_quotas.get(p)
        if q is None:
            continue
        for field, action in (("active_comments", "active_comment"), ("follows", "follow")):
            limit = getattr(q, field, 0) or 0
            if limit and used.get((p, action), 0) >= limit:
                exhausted.append(f"{Platform(p).display_name}{'评论' if action == 'active_comment' else '关注'}")
    items.append(_item(6, "每日配额有剩余", "warn" if exhausted else "pass",
                       f"今日已用完：{', '.join(exhausted)}" if exhausted else "配额充足",
                       "operations" if exhausted else None, "rhythm" if exhausted else None))

    # 7 数据库可写
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        items.append(_item(7, "数据库可写", "pass", "可获取写锁"))
    except Exception as exc:  # noqa: BLE001
        items.append(_item(7, "数据库可写", "block", f"写锁失败：{exc}"))

    # 8 Easel skills
    vendor = Path(__file__).resolve().parent.parent / "vendor" / "easel"
    need = ["web_publisher.py", "login_qr.py", "login_state.py", "platform_readback.py", "account_stats.py"]
    lack = [n for n in need if not (vendor / n).exists()]
    items.append(_item(8, "Easel Skills 可用", "block" if lack else "pass",
                       f"缺失：{', '.join(lack)}" if lack else f"{len(need)} 个脚本就绪"))

    # 9 访问口令（P0 安全）
    from .auth import password_configured
    if password_configured():
        items.append(_item(9, "访问保护已开启", "pass", "接口需要登录后才能调用", "settings", "access"))
    else:
        items.append(_item(9, "访问保护已开启", "warn",
                           "未设置访问口令，公网部署时任何人都能操作账号和配置", "settings", "access"))

    # 10 中文字体（跨平台可移植）
    from .content_pipeline import resolve_font_path
    font = resolve_font_path()
    if font:
        items.append(_item(10, "中文字体可用", "pass", "图片卡片可正常渲染中文", "settings", "appearance"))
    else:
        items.append(_item(10, "中文字体可用", "block",
                           "未找到中文字体，图片卡片会显示方块；请安装 Noto Sans CJK 或在设置里指定字体路径",
                           "settings", "appearance"))

    # 11 通知通道
    from .notify import macos_available
    channels = cfg.notifications.channels
    deliverable: list[str] = []
    if macos_available() and getattr(channels.get("macos"), "enabled", False):
        deliverable.append("macOS 通知")
    for name, env in (("wecom", "WECOM_WEBHOOK_URL"), ("feishu", "FEISHU_WEBHOOK_URL")):
        ccfg = channels.get(name)
        if ccfg and ccfg.enabled and os.getenv(getattr(ccfg, "webhook_url_env", env) or env):
            deliverable.append("企微" if name == "wecom" else "飞书")
    if deliverable:
        items.append(_item(11, "通知通道可用", "pass", f"已启用：{'、'.join(deliverable)}", "settings", "notify"))
    elif macos_available():
        items.append(_item(11, "通知通道可用", "warn",
                           "macOS 通知未开启；草稿保存后不会提醒你发布", "settings", "notify"))
    else:
        items.append(_item(11, "通知通道可用", "warn",
                           "当前系统不支持 macOS 通知；可在设置里填写企微 / 飞书 Webhook",
                           "settings", "notify"))

    worst = "block" if any(i["status"] == "block" for i in items) else (
        "warn" if any(i["status"] == "warn" for i in items) else "pass")
    return {"overall": worst, "items": items, "checked_at": datetime.now().isoformat(timespec="seconds")}


def _has_column(conn, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))
