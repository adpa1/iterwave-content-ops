"""账号画像解析：全局画像 + 平台画像 → 某个平台实际生效的画像。

场景：同一个人在小红书和视频号登录的可能是两个定位不同的账号，
所以文案生成、终极目标检查、热点匹配、选题定位分都要按「平台生效画像」来做，
而不是只看一份全局 persona_keywords。

规则（goal_mode）：
- unified：所有平台都用全局画像（平台画像被忽略，只保留 account_name 展示）。
- per_platform / hybrid：平台配置了画像则按 inherit_global 合并或替换；没配置则回落全局。
- 红线词库永远 = 全局红线 ∪ 平台额外红线，任何模式下都不能被平台画像去掉（安全底线）。
"""
from __future__ import annotations

from typing import Any

from .config import Config, PlatformPersonaConfig

GOAL_MODES = ("unified", "per_platform", "hybrid")
PLATFORM_ROLES = ("", "traffic", "conversion", "brand", "community", "test")
LINK_RELATIONS = ("traffic", "reuse", "cross")
PLATFORM_KEYS = ("xhs", "douyin", "kuaishou", "wechat_channels")


def _dedup(items: list[str]) -> list[str]:
    out: list[str] = []
    for x in items:
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return out


def platform_persona(cfg: Config, platform: str) -> PlatformPersonaConfig | None:
    raw = (cfg.ultimate_goal.platform_personas or {}).get(platform)
    if raw is None:
        return None
    if isinstance(raw, PlatformPersonaConfig):
        return raw
    if isinstance(raw, dict):
        return PlatformPersonaConfig.model_validate(raw)
    return None


def _has_own_persona(pp: PlatformPersonaConfig | None) -> bool:
    return bool(pp and (pp.persona_keywords or pp.forbidden_directions or pp.positioning))


def effective_persona(cfg: Config, platform: str | None) -> dict[str, Any]:
    """返回某平台生效画像；platform=None → 全局画像。"""
    ug = cfg.ultimate_goal
    base = {
        "platform": platform,
        "source": "global",
        "positioning": ug.positioning or "",
        "audience": "",
        "role": "",
        "account_name": "",
        "persona_keywords": list(ug.persona_keywords),
        "forbidden_directions": list(ug.forbidden_directions),
        "redline_keywords": list(ug.redline_keywords),
    }
    if not platform:
        return base
    pp = platform_persona(cfg, platform)
    if pp is None:
        return base
    base["account_name"] = pp.account_name
    base["redline_keywords"] = _dedup(list(ug.redline_keywords) + list(pp.extra_redlines))
    if ug.goal_mode == "unified" or not _has_own_persona(pp):
        return base
    base["audience"] = pp.audience
    base["role"] = pp.role
    if pp.inherit_global:
        base["source"] = "merged"
        base["positioning"] = pp.positioning or base["positioning"]
        base["persona_keywords"] = _dedup(list(ug.persona_keywords) + list(pp.persona_keywords))
        base["forbidden_directions"] = _dedup(list(ug.forbidden_directions) + list(pp.forbidden_directions))
    else:
        base["source"] = "platform"
        base["positioning"] = pp.positioning
        base["persona_keywords"] = _dedup(list(pp.persona_keywords)) or list(ug.persona_keywords)
        base["forbidden_directions"] = _dedup(list(pp.forbidden_directions))
    return base


def all_persona_keywords(cfg: Config, platforms: list[str] | None = None) -> list[str]:
    """所有（启用）平台生效画像关键词的并集：热点匹配 / 选题定位分用。"""
    plats = platforms if platforms is not None else cfg.platforms.enabled_keys()
    kws = list(cfg.ultimate_goal.persona_keywords)
    for p in plats:
        kws += effective_persona(cfg, p)["persona_keywords"]
    return _dedup(kws)


def all_forbidden(cfg: Config, platforms: list[str] | None = None) -> list[str]:
    """禁止方向取「所有平台都禁止」的交集之外的并集会过严；这里按每个平台各自判断，
    汇总视图只返回全局禁区 + 各平台禁区的并集用于展示。"""
    plats = platforms if platforms is not None else cfg.platforms.enabled_keys()
    out = list(cfg.ultimate_goal.forbidden_directions)
    for p in plats:
        out += effective_persona(cfg, p)["forbidden_directions"]
    return _dedup(out)


def matching_platforms(cfg: Config, text: str, platforms: list[str] | None = None) -> list[str]:
    """文本命中哪些平台的生效画像关键词（且未落入该平台禁区）。"""
    plats = platforms if platforms is not None else cfg.platforms.enabled_keys()
    hit: list[str] = []
    for p in plats:
        ep = effective_persona(cfg, p)
        if any(f and f in text for f in ep["forbidden_directions"]):
            continue
        if any(k and k in text for k in ep["persona_keywords"]):
            hit.append(p)
    return hit


def personas_view(cfg: Config) -> dict[str, Any]:
    """画像页展示：模式、全局画像、每个平台的配置值与生效值、平台协同关系。"""
    ug = cfg.ultimate_goal
    platforms: dict[str, Any] = {}
    for p in PLATFORM_KEYS:
        pp = platform_persona(cfg, p) or PlatformPersonaConfig()
        platforms[p] = {
            "config": pp.model_dump(),
            "effective": effective_persona(cfg, p),
            "has_own": _has_own_persona(pp),
        }
    return {
        "goal_mode": ug.goal_mode,
        "global": {
            "positioning": ug.positioning,
            "persona_keywords": list(ug.persona_keywords),
            "forbidden_directions": list(ug.forbidden_directions),
            "redline_keywords_count": len(ug.redline_keywords),
        },
        "platforms": platforms,
        "links": [
            (l.model_dump() if hasattr(l, "model_dump") else dict(l))
            for l in (ug.platform_links or [])
        ],
        "enabled_platforms": cfg.platforms.enabled_keys(),
    }
