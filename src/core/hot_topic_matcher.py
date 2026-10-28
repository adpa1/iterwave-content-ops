"""热点 × 账号标签匹配（热点雷达「与目标的关联」）。

为什么不是简单的关键词子串匹配：热榜标题几乎不会原样出现「AI教育」「效率工具」这类定位词，
原实现 106 条热点 0 命中，dimension_match 全部为 NULL，雷达等于没有做标签匹配。

两级匹配，结果落库（hot_topics.match_*），由后台采集任务调用，GET 接口只读库：
  1. 确定性规则（始终执行）
     - 红线词命中 → 不可发布（R22，沿用原逻辑）
     - 禁止方向 / 定位关键词 / 内容维度 命中 → 记录命中词
     - 无 LLM 时：命中定位词或维度 → match_score=60（method=keyword），未命中 → 保持 NULL「待研判」，
       绝不把「没命中关键词」当成 0 分（R3：未知 ≠ 0）
  2. LLM 语义研判（config.llm.enabled 且有 API Key 时）
     - 按批（≤20 条）把标题 + 账号画像（定位词 / 禁止方向 / 内容维度）交给模型，
       输出 0–100 关联分、最贴近的维度、可切入角度、理由、是否落入禁止方向
     - 写 ai_calls 审计（step=hot_match）
  - match_version = 画像签名，画像或 prompt 变化后旧结果自动视为过期并重判。

判定口径（供 hot_topics._load 使用）：
  dimension_match = True   match_score >= MATCH_THRESHOLD 且未落入禁止方向
                  = False  已研判且（分数不足 或 落入禁止方向）
                  = None   尚未研判
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PROMPT_VERSION = "hot_match_v1"
MATCH_THRESHOLD = 60.0
KEYWORD_SCORE = 60.0
BATCH_SIZE = 20
MAX_PER_RUN = 200

# LLM 调用函数签名：(system_prompt, user_prompt) -> (content, usage)
LlmFn = Callable[[str, str], tuple[str, dict]]


@dataclass
class Persona:
    keywords: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    redlines: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)

    def signature(self) -> str:
        raw = json.dumps({
            "k": sorted(self.keywords), "f": sorted(self.forbidden),
            "d": sorted(self.dimensions), "v": PROMPT_VERSION,
        }, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load_persona(cfg, conn) -> Persona:
    """账号画像：config.ultimate_goal + 母计划维度 + 选题库高频维度。"""
    ug = cfg.ultimate_goal
    dims: list[str] = []
    try:
        from .plan_version import PlanVersionManager
        plan = PlanVersionManager(cfg, conn).load_plan()[0]
        dims.extend((plan.get("dimension_allocation") or {}).keys())
    except Exception:  # noqa: BLE001 — 没有母计划时只用画像关键词
        pass
    try:
        rows = conn.execute(
            "SELECT dimension, COUNT(*) AS n FROM topic_index WHERE dimension IS NOT NULL "
            "AND dimension != '' GROUP BY dimension ORDER BY n DESC LIMIT 12"
        ).fetchall()
        dims.extend(r["dimension"] for r in rows)
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    dims = [d for d in dims if d and not (d in seen or seen.add(d))]
    return Persona(
        keywords=[k for k in (ug.persona_keywords or []) if k],
        forbidden=[k for k in (getattr(ug, "forbidden_directions", None) or []) if k],
        redlines=[k for k in (ug.redline_keywords or []) if k],
        dimensions=dims,
    )


def default_llm_fn(cfg) -> Optional[LlmFn]:
    """config.llm 可用时返回调用函数，否则 None（自动降级为关键词匹配）。"""
    llm = getattr(cfg, "llm", None)
    if not llm or not getattr(llm, "enabled", False):
        return None
    api_key = os.getenv(getattr(llm, "api_key_env", "ARK_API_KEY"))
    if not api_key:
        return None

    def _call(system_prompt: str, user_prompt: str) -> tuple[str, dict]:
        import httpx
        with httpx.Client(timeout=llm.timeout_seconds) as client:
            resp = client.post(
                f"{llm.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": llm.model,
                    "messages": [{"role": "system", "content": system_prompt},
                                 {"role": "user", "content": user_prompt}],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"], data.get("usage") or {}

    return _call


SYSTEM_PROMPT = (
    "你是内容账号的选题研判助手。判断每条热点与账号定位的关联度，只输出 JSON，不要解释。"
    "评分标准：80-100 可直接围绕账号定位做内容；60-79 需要找角度但能自然关联；"
    "30-59 牵强；0-29 无关。落入禁止方向的一律 forbidden=true。"
    "政治、灾难、社会负面事件除非与账号定位直接相关，否则视为无关。"
    'JSON 结构：{"items":[{"id":string,"score":number,"dimension":string,'
    '"angle":string,"reason":string,"forbidden":boolean}]}'
)


def _user_prompt(persona: Persona, batch: list[dict]) -> str:
    lines = [f'{{"id":"{r["id"]}","title":{json.dumps(r["title"], ensure_ascii=False)}}}' for r in batch]
    return (
        f"账号定位关键词：{'、'.join(persona.keywords) or '未配置'}\n"
        f"内容维度：{'、'.join(persona.dimensions) or '未配置'}\n"
        f"禁止方向：{'、'.join(persona.forbidden) or '无'}\n"
        f"dimension 字段请从内容维度中选最贴近的一个，都不贴近填空串；angle 用一句话给出可切入角度，无关填空串。\n"
        f"热点列表：\n" + "\n".join(lines)
    )


def keyword_judgement(persona: Persona, title: str) -> dict:
    hits_kw = [k for k in persona.keywords if k in title]
    hits_dim = [d for d in persona.dimensions if d in title]
    hits_forbidden = [k for k in persona.forbidden if k in title]
    return {"keyword_hits": hits_kw + [d for d in hits_dim if d not in hits_kw],
            "dimension": (hits_dim[0] if hits_dim else None),
            "forbidden": bool(hits_forbidden)}


class HotTopicMatcher:
    def __init__(self, cfg, conn, llm_fn: Optional[LlmFn] = None, *, use_default_llm: bool = True):
        self.cfg = cfg
        self.conn = conn
        self.llm_fn = llm_fn if llm_fn is not None else (default_llm_fn(cfg) if use_default_llm else None)
        self.persona = load_persona(cfg, conn)

    @property
    def version(self) -> str:
        return self.persona.signature() + (":llm" if self.llm_fn else ":kw")

    def pending(self, limit: int = MAX_PER_RUN) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, title FROM hot_topics WHERE evidence_status = 'collected' "
            "AND title IS NOT NULL AND duplicate_of IS NULL "
            "AND (match_version IS NULL OR match_version != ?) "
            "ORDER BY heat_score DESC NULLS LAST LIMIT ?",
            (self.version, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def run(self, limit: int = MAX_PER_RUN) -> dict:
        rows = self.pending(limit)
        report = {"pending": len(rows), "llm": 0, "keyword": 0, "failed_batches": 0,
                  "method": "llm" if self.llm_fn else "keyword"}
        if not rows:
            return report
        if self.llm_fn is None:
            for r in rows:
                self._save_keyword(r)
                report["keyword"] += 1
            self.conn.commit()
            return report
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            try:
                results = self._llm_batch(batch)
            except Exception as exc:  # noqa: BLE001 — 单批失败不影响其他批，该批保持「待研判」
                logger.warning("热点 LLM 研判失败（%d 条）：%s", len(batch), exc)
                report["failed_batches"] += 1
                continue
            for r in batch:
                res = results.get(r["id"])
                if res is None:
                    continue  # 模型漏掉的条目保持待研判，下次重试
                self._save(r, res, method="llm")
                report["llm"] += 1
            self.conn.commit()
        return report

    # ------------------------------------------------------------------

    def _save_keyword(self, row: dict) -> None:
        kj = keyword_judgement(self.persona, row["title"])
        if kj["forbidden"]:
            res = {"score": 0.0, "dimension": kj["dimension"], "angle": "",
                   "reason": "标题命中禁止方向", "forbidden": True}
        elif kj["keyword_hits"]:
            res = {"score": KEYWORD_SCORE, "dimension": kj["dimension"], "angle": "",
                   "reason": f"命中定位词/维度：{'、'.join(kj['keyword_hits'])}", "forbidden": False}
        else:
            # 未命中 ≠ 无关：没有语义研判能力时保持 NULL（待研判），只记录已检查过
            res = {"score": None, "dimension": None, "angle": "", "reason": "", "forbidden": False}
        self._save(row, res, method="keyword")

    def _save(self, row: dict, res: dict, *, method: str) -> None:
        self.conn.execute(
            "UPDATE hot_topics SET match_score=?, match_dimension=?, match_angle=?, match_reason=?, "
            "match_method=?, match_version=?, matched_at=?, forbidden_hit=? WHERE id=?",
            (res.get("score"), res.get("dimension") or None, res.get("angle") or None,
             res.get("reason") or None, method, self.version,
             datetime.now().isoformat(timespec="seconds"),
             1 if res.get("forbidden") else 0, row["id"]),
        )

    def _llm_batch(self, batch: list[dict]) -> dict[str, dict]:
        started = time.monotonic()
        ok, valid, usage = False, False, {}
        try:
            content, usage = self.llm_fn(SYSTEM_PROMPT, _user_prompt(self.persona, batch))
            ok = True
            parsed = json.loads(content)
            out: dict[str, dict] = {}
            ids = {r["id"] for r in batch}
            for it in parsed.get("items") or []:
                tid = str(it.get("id") or "")
                if tid not in ids:
                    continue
                try:
                    score = max(0.0, min(100.0, float(it.get("score"))))
                except (TypeError, ValueError):
                    continue
                dim = str(it.get("dimension") or "").strip()
                if dim and self.persona.dimensions and dim not in self.persona.dimensions:
                    dim = ""
                out[tid] = {
                    "score": score, "dimension": dim,
                    "angle": str(it.get("angle") or "").strip()[:120],
                    "reason": str(it.get("reason") or "").strip()[:200],
                    "forbidden": bool(it.get("forbidden")),
                }
            valid = bool(out)
            return out
        finally:
            self._log_ai_call(len(batch), started, ok, valid, usage)

    def _log_ai_call(self, n: int, started: float, success: bool, valid: bool, usage: dict) -> None:
        ms = int((time.monotonic() - started) * 1000)
        try:
            self.conn.execute(
                "INSERT INTO ai_calls (step, module, model, prompt_version, input_digest, "
                "tokens_in, tokens_out, latency_ms, duration_ms, output_valid, success, called_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("hot_match", "hot_topic_matcher", getattr(getattr(self.cfg, "llm", None), "model", ""),
                 PROMPT_VERSION, f"batch={n};persona={self.persona.signature()}",
                 usage.get("prompt_tokens"), usage.get("completion_tokens"), ms, ms,
                 1 if valid else 0, 1 if success else 0, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
        except Exception:  # noqa: BLE001 — 审计失败不影响研判
            logger.debug("ai_calls 写入失败", exc_info=True)


def dimension_match_of(row, current_version: Optional[str]) -> Optional[bool]:
    """按落库结果给出三态 dimension_match（画像已变化的旧结果视为未研判）。"""
    keys = row.keys() if hasattr(row, "keys") else row
    if "match_version" not in keys or row["match_version"] is None:
        return None
    if current_version and row["match_version"] != current_version:
        return None
    if row["forbidden_hit"]:
        return False
    score = row["match_score"]
    if score is None:
        return None
    return score >= MATCH_THRESHOLD
