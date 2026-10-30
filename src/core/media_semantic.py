"""Optional vision-model ranking for approved media assets.

Uses an OpenAI-compatible chat-completions endpoint only when OPENAI_API_KEY is configured.
Failure is deliberately non-fatal: callers retain deterministic keyword recommendations.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx


def rank_media_with_vision(
    *, title: str, body: str, usage_role: str, candidates: list[dict[str, Any]],
    media_dir: Path, cfg=None,
) -> list[dict[str, Any]] | None:
    if cfg is None:
        from src.core.config import get_config
        cfg = get_config()
    policy = cfg.vision_ranking
    # Image bytes leave the local machine only when explicitly enabled by the operator.
    enabled = os.getenv("ITERWAVE_ENABLE_VISION_MEDIA_RANKING", "").strip().lower() in ("1", "true", "yes")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not enabled or not api_key or not candidates:
        return None

    blocks: list[dict[str, Any]] = []
    allowed_ids: set[int] = set()
    for item in candidates[:min(policy.candidate_limit, 32)]:
        path = (media_dir / str(item.get("stored_filename") or "")).resolve()
        if not path.is_relative_to(media_dir.resolve()) or not path.is_file():
            continue
        raw = path.read_bytes()
        if not raw or len(raw) > min(policy.max_image_bytes, 4 * 1024 * 1024):
            continue
        suffix = path.suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png" if suffix == ".png" else "image/webp"
        asset_id = int(item["asset_id"])
        allowed_ids.add(asset_id)
        blocks.append({"type": "text", "text": f"图片ID={asset_id}；素材名称={item.get('title') or ''}"})
        blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", "detail": "low"}})
    if not allowed_ids:
        return None

    prompt = (
        f"请为内容选择最合适的图片，用途={usage_role}。标题={title}。正文={body[:policy.max_body_chars]}。"
        "只返回JSON对象，格式为{\"ranked_ids\":[数字ID按相关性降序],\"reasons\":{\"数字ID\":\"简短理由\"}}。"
        "只能选择给定的图片ID；如果不相关请不要选。"
    )
    response = httpx.post(
        os.getenv("ITERWAVE_VISION_API_BASE", policy.base_url).rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": os.getenv("ITERWAVE_VISION_MODEL", policy.model),
              "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, *blocks]}],
              "temperature": policy.temperature, "max_tokens": policy.max_tokens},
        timeout=policy.timeout_seconds,
    )
    response.raise_for_status()
    answer = response.json()["choices"][0]["message"]["content"]
    if isinstance(answer, list):
        answer = "".join(str(part.get("text", "")) for part in answer if isinstance(part, dict))
    match = re.search(r"\{.*\}", str(answer), re.S)
    if not match:
        return None
    result = json.loads(match.group(0))
    reasons = result.get("reasons", {})
    if not isinstance(reasons, dict):
        reasons = {}
    ranked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for value in result.get("ranked_ids", []):
        try:
            asset_id = int(value)
        except (TypeError, ValueError):
            continue
        if asset_id not in allowed_ids or asset_id in seen:
            continue
        seen.add(asset_id)
        ranked.append({
            "asset_id": asset_id,
            "reason": "AI视觉语义匹配：" + str(reasons.get(str(asset_id), "模型判断与内容相关"))[:180],
        })
    return ranked or None
