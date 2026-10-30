"""
内容生产线 — 文案生成 + 图片卡片制作（§3 / §5）。

R2 红线：本模块只负责 G2 之前的产物生成，**所有文案必须交给
UltimateGoalChecker.check_content() 检查后才能进入卡片制作**（由
RoundExecutor 串行编排，本模块不自行放行）。

LLM 策略：
- config.llm.enabled=true 且环境变量有 key → 调用 Agent Plan（ARK）生成
- 否则 → 正式生成明确失败；模板只保留为显式演示素材，不进入生产链路
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from ..adapters.base import Platform

logger = logging.getLogger(__name__)

class ContentGenerationError(RuntimeError):
    """模型生成失败；携带稳定错误码和可安全展示给用户的提示。"""

    def __init__(
        self, code: str, message: str, *, retryable: bool = False,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds




# =====================================================================
# 平台文案风格（migrations/016_platform_styles.sql，Settings 页可编辑）
# =====================================================================

# DB 不可用时的兜底：只给通用结构，不带任何垂类标签。
# 话题标签属于账号运营数据，必须来自库（platform_styles.hashtags），
# 不能把某个垂类的话题写死进代码。
_FALLBACK_STYLE: dict = {
    "title_style": "简洁直接，突出价值点",
    "body_style": "分段清晰，结尾引导互动",
    "tone": "自然、口语化",
    "hashtags": [],
}


def load_platform_style(platform: Platform, conn=None) -> dict:
    """读取平台文案风格：platform_styles 表 → 通用兜底。

    返回 {"title_style", "body_style", "tone", "hashtags"}，
    hashtags 为不带 # 前缀的标签列表（可能为空，空表示只带选题维度标签）。
    """
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT title_style, body_style, tone, hashtags "
                "FROM platform_styles WHERE platform = ?",
                (platform.value,),
            ).fetchone()
        except Exception:  # noqa: BLE001 — 老库无表时退回兜底，不中断文案生成
            logger.warning(
                "platform_styles 读取失败，使用通用兜底 platform=%s", platform.value
            )
            row = None
        if row is not None:
            try:
                raw_tags = json.loads(row[3] or "[]")
            except (TypeError, ValueError):
                raw_tags = []
            tags = [
                str(t).strip().lstrip("#")
                for t in (raw_tags if isinstance(raw_tags, list) else [])
                if str(t).strip()
            ]
            return {
                "title_style": str(row[0] or _FALLBACK_STYLE["title_style"]),
                "body_style": str(row[1] or _FALLBACK_STYLE["body_style"]),
                "tone": str(row[2] or _FALLBACK_STYLE["tone"]),
                "hashtags": tags,
            }
    return {
        "title_style": _FALLBACK_STYLE["title_style"],
        "body_style": _FALLBACK_STYLE["body_style"],
        "tone": _FALLBACK_STYLE["tone"],
        "hashtags": [],
    }


# =====================================================================
# 文案生成
# =====================================================================

def generate_copy_content(
    topic_title: str,
    topic_summary: str,
    dimension: str,
    platform: Platform,
    cfg=None,
    conn=None,
    topic_id: Optional[str] = None,
    round_id: Optional[int] = None,
    reject_reason: Optional[str] = None,
    strategy_hints: Optional[list[str]] = None,
) -> tuple[str, str, list[str]]:
    """生成单平台文案。正式生成必须由可用模型完成，失败时明确抛出业务错误。

    返回 (title, body, tags)。模板只允许作为显式 demo 素材，不进入正式生成链路。

    reject_reason：上一版被 G2 拒收的原因；LLM 会收到改写要求。
    conn 不为空时，LLM 调用会写 ai_calls 审计表（tokens/latency/success）。
    """
    style = load_platform_style(platform, conn)
    if cfg is None or not _llm_available(cfg):
        raise ContentGenerationError(
            "model_not_configured",
            "尚未配置可用模型，请先配置模型服务和 API Key 后再生成",
        )

    try:
        result = _generate_via_llm(
            cfg, topic_title, topic_summary, dimension, platform, style,
            conn=conn, topic_id=topic_id, round_id=round_id,
            reject_reason=reject_reason, strategy_hints=strategy_hints,
        )
    except ContentGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 — 未分类异常也必须 fail closed
        logger.exception("LLM 文案生成失败 platform=%s", platform.value)
        raise ContentGenerationError(
            "model_unavailable", "模型服务暂时不可用，请检查服务状态后重试",
            retryable=True,
        ) from exc

    if not result:
        raise ContentGenerationError(
            "model_output_invalid", "模型未返回有效文案，请调整要求后重试",
            retryable=True,
        )
    return result


def build_hint_block(strategy_hints: Optional[list[str]], policy=None) -> str:
    """上一轮复盘要点 → 提示词片段。要点已在 autogrow 里过了合规方向检查；
    这里只作参考，明确不得为迎合数据脱离方向或触碰红线。"""
    if policy is None:
        from .config import CopyGenerationConfig
        policy = CopyGenerationConfig()
    hints = [h.strip()[:policy.strategy_hint_max_chars] for h in (strategy_hints or [])
             if h and h.strip()][:policy.strategy_hint_max_count]
    if not hints:
        return ""
    lines = "\n".join(f"- {h}" for h in hints)
    return (
        f"\n上一轮复盘要点（参考，不得为迎合数据脱离内容方向或触碰红线）：\n{lines}"
    )


def _llm_available(cfg) -> bool:
    llm = getattr(cfg, "llm", None)
    if not llm or not getattr(llm, "enabled", False):
        return False
    return bool(os.getenv(getattr(llm, "api_key_env", "ARK_API_KEY")))


def _generate_via_llm(
    cfg, topic_title: str, topic_summary: str, dimension: str, platform: Platform,
    style: dict,
    conn=None, topic_id: Optional[str] = None, round_id: Optional[int] = None,
    reject_reason: Optional[str] = None,
    strategy_hints: Optional[list[str]] = None,
) -> Optional[tuple[str, str, list[str]]]:
    """调用 Agent Plan（ARK）Chat Completions，强制 JSON 输出。"""
    import httpx

    llm = cfg.llm
    api_key = os.getenv(llm.api_key_env)
    if not api_key:
        return None

    system_prompt = (
        "你是四平台图文运营文案专家。只输出 JSON，不要任何解释文字。"
        "JSON 结构：{\"title\": string, \"body\": string, \"tags\": string[]}"
    )
    user_prompt = (
        f"平台：{platform.display_name}\n"
        f"平台风格：{style['title_style']}；{style['body_style']}；语气 {style['tone']}\n"
        f"选题：{topic_title}\n"
        f"摘要：{topic_summary[:cfg.copy_generation.topic_summary_max_chars]}\n"
        f"内容方向：{dimension}\n"
        f"要求：标题不超过 {cfg.copy_generation.title_max_chars} 字；正文 {cfg.copy_generation.body_min_chars}-{cfg.copy_generation.body_max_chars} 字；标签 {cfg.copy_generation.tag_min_count}-{cfg.copy_generation.tag_max_count} 个；"
        f"不得出现违法违规内容；不得脱离「{dimension}」方向；"
        f"不得编造事实、产品、公司、数据、奖项或用户评价。"
    )
    # 账号画像：按平台生效画像（不同平台可能是不同账号、不同定位）
    if getattr(cfg, "ultimate_goal", None) is not None:
        from .persona import effective_persona
        ep = effective_persona(cfg, getattr(platform, "value", str(platform)))
        kws = [k for k in ep["persona_keywords"] if k]
        forb = [k for k in ep["forbidden_directions"] if k]
        if ep.get("positioning"):
            user_prompt += f"\n本平台账号定位：{ep['positioning']}"
        if ep.get("audience"):
            user_prompt += f"\n目标受众：{ep['audience']}"
        role_hint = {
            "traffic": "本平台承担引流职责：内容要有钩子，引导关注主页或其它平台。",
            "conversion": "本平台承担转化职责：内容要给出明确的下一步行动。",
            "brand": "本平台承担品牌职责：保持专业一致的人设与调性。",
            "community": "本平台承担社群职责：多引导评论互动。",
            "test": "本平台用于试验新方向：可以尝试新的切入角度。",
        }.get(ep.get("role") or "")
        if role_hint:
            user_prompt += f"\n{role_hint}"
        if kws:
            user_prompt += (
                f"\n账号定位关键词：{'、'.join(kws[:cfg.copy_generation.persona_keyword_limit])}；"
                f"标题或正文必须自然出现其中至少 1 个，不得生硬堆砌。"
            )
        if forb:
            user_prompt += f"\n禁止方向：{'；'.join(forb[:cfg.copy_generation.forbidden_direction_limit])}。"
    prompt_hints = build_hint_block(strategy_hints, cfg.copy_generation)
    if prompt_hints:
        user_prompt += prompt_hints
    if reject_reason:
        user_prompt += (
            f"\n上一版文案被合规检查拒收，原因：{reject_reason}。"
            f"请改写并规避该问题，不要重复同样的表达。"
        )

    payload = {
        "model": llm.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": llm.temperature,
        "response_format": {"type": "json_object"},
    }
    started = time.monotonic()
    client = None
    try:
        client = httpx.Client(timeout=llm.timeout_seconds)
        resp = client.post(
            f"{llm.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException as exc:
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens_in=None, tokens_out=None, success=False, output_valid=False,
        )
        raise ContentGenerationError(
            "model_timeout", "模型请求超时，请稍后重试", retryable=True,
        ) from exc
    except httpx.HTTPStatusError as exc:
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens_in=None, tokens_out=None, success=False, output_valid=False,
        )
        status = exc.response.status_code
        if status in (401, 403):
            code, message, retryable = "model_auth_failed", "模型鉴权失败，请检查 API Key", False
        elif status == 402:
            code, message, retryable = "model_quota_exceeded", "模型额度不足，请检查账户额度", False
        elif status == 429:
            retry_after_header = exc.response.headers.get("Retry-After")
            try:
                retry_after_seconds = max(0.1, min(float(retry_after_header), 300.0)) if retry_after_header else None
            except (TypeError, ValueError):
                retry_after_seconds = None
            code, message, retryable = (
                "model_rate_limited", "模型请求过于频繁，请稍后重试",
                retry_after_seconds is not None,
            )
        elif status in (408, 504):
            code, message, retryable = "model_timeout", "模型请求超时，请稍后重试", True
        elif status >= 500:
            code, message, retryable = "model_unavailable", "模型服务暂时不可用，请稍后重试", True
        else:
            code, message, retryable = "model_request_failed", "模型请求失败，请检查模型配置", False
        raise ContentGenerationError(
            code, message, retryable=retryable,
            retry_after_seconds=locals().get("retry_after_seconds"),
        ) from exc
    except httpx.RequestError as exc:
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens_in=None, tokens_out=None, success=False, output_valid=False,
        )
        raise ContentGenerationError(
            "model_unavailable", "无法连接模型服务，请检查网络或服务地址",
            retryable=True,
        ) from exc
    except Exception as exc:
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens_in=None, tokens_out=None, success=False, output_valid=False,
        )
        raise ContentGenerationError(
            "model_unavailable", "模型服务暂时不可用，请检查服务状态后重试",
            retryable=True,
        ) from exc
    finally:
        if client is not None:
            client.close()

    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=latency_ms, tokens_in=None, tokens_out=None,
            success=False, output_valid=False,
        )
        raise ContentGenerationError(
            "model_output_invalid", "模型返回结构不完整，请重试", retryable=True,
        )

    usage = data.get("usage") or {}
    tokens_in = usage.get("prompt_tokens")
    tokens_out = usage.get("completion_tokens")
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
            success=False, output_valid=False,
        )
        raise ContentGenerationError(
            "model_output_invalid", "模型返回内容格式不正确，请重试", retryable=True,
        )

    if not isinstance(parsed, dict):
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
            success=False, output_valid=False,
        )
        raise ContentGenerationError(
            "model_output_invalid", "模型返回内容结构不正确，请重试", retryable=True,
        )

    title = parsed.get("title")
    body = parsed.get("body")
    title = title.strip() if isinstance(title, str) else ""
    body = body.strip() if isinstance(body, str) else ""
    output_valid = bool(title and body)
    _log_ai_call(
        conn, platform, llm.model, topic_id, round_id,
        latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
        success=output_valid, output_valid=output_valid,
    )
    if not output_valid:
        raise ContentGenerationError(
            "model_output_invalid", "模型未返回标题或正文，请重试", retryable=True,
        )

    # 标签是可选项；缺失时只使用账号配置的平台标签，不构造模板正文。
    tags = parsed.get("tags")
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\\s]+", tags) if t]
    if not isinstance(tags, list):
        tags = style["hashtags"]
    normalized_tags = [
        f"#{str(tag).strip().lstrip('#')}"
        for tag in tags if str(tag).strip()
    ][:cfg.copy_generation.tag_max_count]
    return (
        title[:cfg.copy_generation.title_max_chars],
        body[:cfg.copy_generation.body_max_chars],
        normalized_tags,
    )


def _log_ai_call(
    conn, platform: Platform, model: str, topic_id: Optional[str],
    round_id: Optional[int], *, latency_ms: int, tokens_in: Optional[int],
    tokens_out: Optional[int], success: bool, output_valid: bool,
) -> None:
    """写 ai_calls 审计（R14 统一字段；失败不影响主流程）。"""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO ai_calls (step, module, model, prompt_version, input_digest, "
            "tokens_in, tokens_out, latency_ms, duration_ms, output_valid, success, called_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "copywriter", "copywriter", model, "v1",
                f"round={round_id};topic={topic_id};platform={platform.value}",
                tokens_in, tokens_out, latency_ms, latency_ms,
                1 if output_valid else 0, 1 if success else 0,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — 审计失败不能中断文案生成
        logger.exception("ai_calls 审计落库失败 platform=%s", platform.value)


def _generate_via_template(
    topic_title: str, topic_summary: str, dimension: str, platform: Platform,
    style: dict,
) -> tuple[str, str, list[str]]:
    """模板降级（无 LLM 时使用，质量低于 LLM 但结构完整）。"""
    summary = (topic_summary or "").strip()
    snippet = summary[:120] + ("…" if len(summary) > 120 else "")

    if platform == Platform.XHS:
        title = f"🔥{topic_title[:16]}，这几点值得记下来"
        body = (
            f"最近在关注「{topic_title}」，整理了几条实操心得：\n\n"
            f"1️⃣ {snippet or '先搞清楚它解决什么问题'}\n\n"
            f"2️⃣ 落到「{dimension}」这个方向，关键是能立刻用起来，而不是收藏吃灰。\n\n"
            f"3️⃣ 建议先挑一个小场景试，跑通再放大。\n\n"
            f"你们平时是怎么做的？评论区聊聊～"
        )
    elif platform == Platform.DOUYIN:
        title = f"别再瞎折腾了！{topic_title[:14]}"
        body = (
            f"{snippet}\n\n"
            f"一句话总结：这件事的核心是「{dimension}」。\n"
            f"别一上来就追求完美，先做出来再优化。\n\n"
            f"觉得有用点个赞，下条讲具体怎么落地。"
        )
    elif platform == Platform.KUAISHOU:
        title = f"{topic_title[:18]}｜说点实在的"
        body = (
            f"{snippet}\n\n"
            f"这事儿不复杂，就三步：先搞清楚现状，再找最小切入点，最后持续做。\n"
            f"方向是「{dimension}」，别跑偏。\n\n"
            f"有同样困惑的评论区扣 1。"
        )
    else:  # 视频号
        title = f"关于{topic_title[:16]}，我的几点判断"
        body = (
            f"{snippet}\n\n"
            f"第一，这件事的价值不在工具本身，而在于它改变了「{dimension}」的工作方式。\n\n"
            f"第二，短期内最大的阻力是习惯迁移成本，而不是技术门槛。\n\n"
            f"第三，真正拉开差距的是持续投入，而不是一次性的工具采购。\n\n"
            f"欢迎就这个判断一起讨论。"
        )

    # 标签只来自「选题维度 + 库里配置的平台标签」，不再有写死的垂类话题
    return title, body, [f"#{dimension}"] + list(style["hashtags"])


# =====================================================================
# 图片卡片制作
# =====================================================================

CARD_SIZE = (1080, 1440)

_PLATFORM_PALETTE: dict[Platform, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    Platform.XHS: ((255, 88, 120), (255, 176, 124)),
    Platform.DOUYIN: ((28, 28, 34), (72, 40, 110)),
    Platform.KUAISHOU: ((255, 126, 40), (255, 196, 84)),
    Platform.WECHAT_CHANNELS: ((18, 122, 96), (56, 176, 140)),
}

_FONT_CANDIDATES = (
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux（Debian/Ubuntu、CentOS、Alpine 常见路径）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)


def font_candidates() -> tuple[str, ...]:
    """可用字体候选：配置 / .env 指定路径优先，再按平台常见路径。"""
    env = os.getenv("ITERWAVE_FONT_PATH", "").strip()
    return ((env,) if env else ()) + _FONT_CANDIDATES


def resolve_font_path() -> Optional[str]:
    """返回第一个真实存在的中文字体文件；都没有则 None（自检会标红）。"""
    for path in font_candidates():
        if Path(path).exists():
            return path
    return None


def _load_font(size: int):
    from PIL import ImageFont

    for path in font_candidates():
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


# emoji / 变体选择符在中文字体里没有字形，会渲染成豆腐块
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]"
)
# 连续 ASCII 字母数字（含常见单位）作为一个不可拆分 token
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9]+[%％]?")

_TRAILING_PUNCT = "，。、；：！？）》」』,.;:!?)]}"


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _tokenize(text: str) -> list[str]:
    """把文本切成不可拆分的 token：ASCII 单词/数字整体，CJK 逐字。"""
    tokens: list[str] = []
    i = 0
    while i < len(text):
        m = _ASCII_TOKEN_RE.match(text, i)
        if m:
            tokens.append(m.group())
            i = m.end()
        else:
            tokens.append(text[i])
            i += 1
    return tokens


def _wrap_cjk(text: str, font, max_width: int) -> list[str]:
    """按像素宽度折行：CJK 逐字断行，ASCII 单词/数字不拆断。"""
    from PIL import Image, ImageDraw

    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)

    lines: list[str] = []
    for raw_line in _strip_emoji(text).split("\n"):
        current = ""
        for token in _tokenize(raw_line):
            candidate = current + token
            if draw.textlength(candidate, font=font) > max_width and current:
                # 行首不留标点：把标点并回上一行
                if token and token[0] in _TRAILING_PUNCT and len(current) > 1:
                    current += token
                    lines.append(current)
                    current = ""
                    continue
                lines.append(current)
                current = token
            else:
                current = candidate
        if current:
            lines.append(current)
    return [line for line in lines if line.strip()]


def make_card(
    title: str,
    subtitle: str,
    output_dir: Path,
    platform: Platform = Platform.XHS,
    footer: str = "",
    page_text: str = "",
) -> Path:
    """生成 1080×1440 图文封面卡（渐变背景 + 折行标题 + 维度标签 + 页脚 + 页码）。

    纯 Pillow 实现，无外部字体下载依赖。R6.5：Pillow 已有，无需自造图像引擎。
    """
    from PIL import Image, ImageDraw

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"card_{platform.value}_{uuid.uuid4().hex[:8]}.png"

    width, height = CARD_SIZE
    top_color, bottom_color = _PLATFORM_PALETTE.get(platform, _PLATFORM_PALETTE[Platform.XHS])

    # 垂直渐变背景
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / (height - 1)
        color = tuple(
            int(top_color[i] + (bottom_color[i] - top_color[i]) * ratio) for i in range(3)
        )
        draw.line([(0, y), (width, y)], fill=color)

    margin = 96
    text_max_width = width - margin * 2

    # 维度标签（胶囊）
    tag_font = _load_font(38)
    tag_text = f"＃{subtitle}" if subtitle else f"＃{os.getenv('CARD_TAG_FALLBACK', '今日分享')}"

    tag_w = draw.textlength(tag_text, font=tag_font)
    tag_box = (margin, margin + 40, margin + tag_w + 56, margin + 110)
    draw.rounded_rectangle(tag_box, radius=35, fill=(255, 255, 255))
    draw.text(
        (margin + 28, margin + 56), tag_text,
        fill=(30, 30, 30), font=tag_font,
    )

    # 主标题（折行，最多 5 行，标题块垂直居中于 360~1180 区间）
    title_font = _load_font(84)
    title_lines = _wrap_cjk(title, title_font, text_max_width)[:5]
    line_height = 112
    block_height = len(title_lines) * line_height
    area_top, area_bottom = 360, 1180
    y_cursor = area_top + max(0, (area_bottom - area_top - block_height) // 2)
    for line in title_lines:
        draw.text((margin, y_cursor), line, fill=(255, 255, 255), font=title_font)
        y_cursor += line_height

    # 分隔线
    draw.line(
        [(margin, y_cursor + 40), (margin + 160, y_cursor + 40)],
        fill=(255, 255, 255), width=6,
    )

    # 页脚（留空则不绘制，避免把某个垂类的品牌语写死进所有账号）
    if footer.strip():
        footer_font = _load_font(36)
        draw.text(
            (margin, height - margin - 60), footer,
            fill=(255, 255, 255), font=footer_font,
        )

    # 页码（多图卡组时显示「1/6」，单图不显示）
    if page_text:
        page_font = _load_font(34)
        draw.text(
            (width - margin - draw.textlength(page_text, font=page_font),
             height - margin - 56),
            page_text, fill=(255, 255, 255), font=page_font,
        )

    img.save(str(output_path), format="PNG", optimize=True)
    logger.info("🖼️ 图片卡片生成: %s (%dx%d)", output_path, width, height)
    return output_path


def _render_body_page(
    lines: list[str], output_dir: Path, platform: Platform, footer: str, page_text: str,
) -> Path:
    """正文分页卡：同色系渐变背景 + 大字号正文 + 页脚 + 页码（1080×1440）。"""
    from PIL import Image, ImageDraw

    output_path = output_dir / f"card_{platform.value}_{uuid.uuid4().hex[:8]}.png"
    width, height = CARD_SIZE
    top_color, bottom_color = _PLATFORM_PALETTE.get(platform, _PLATFORM_PALETTE[Platform.XHS])
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / (height - 1)
        color = tuple(
            int(top_color[i] + (bottom_color[i] - top_color[i]) * ratio) for i in range(3)
        )
        draw.line([(0, y), (width, y)], fill=color)

    margin = 96
    body_font = _load_font(52)
    # 内容少时垂直居中，避免整页文字堆在顶部、下半页空白
    block_height = len(lines) * 82
    y_cursor = max(margin + 40, (height - block_height) // 2 - 60)
    for line in lines:
        draw.text((margin, y_cursor), line, fill=(255, 255, 255), font=body_font)
        y_cursor += 82

    if footer.strip():
        footer_font = _load_font(36)
        draw.text((margin, height - margin - 60), footer, fill=(255, 255, 255), font=footer_font)
    page_font = _load_font(34)
    draw.text(
        (width - margin - draw.textlength(page_text, font=page_font), height - margin - 56),
        page_text, fill=(255, 255, 255), font=page_font,
    )

    img.save(str(output_path), format="PNG", optimize=True)
    logger.info("🖼️ 正文卡片生成: %s", output_path)
    return output_path


def card_footer(cfg, platform: Platform) -> str:
    """卡片页脚跟随账号画像：优先本平台账号名 + 定位，其次全局定位。

    留空则卡片不画页脚（不写死任何垂类文案）。生成与重生成卡片共用。
    """
    goal = cfg.ultimate_goal
    persona = goal.platform_personas.get(platform.value)
    name = (persona.account_name if persona else "").strip()
    positioning = (
        (persona.positioning if persona else "").strip() or goal.positioning.strip()
    )
    text = " · ".join(part for part in (name, positioning) if part)
    return text[:30]


def make_card_set(
    title: str,
    subtitle: str,
    body: str,
    output_dir: Path,
    platform: Platform = Platform.XHS,
    footer: str = "",
    max_pages: int = 9,
) -> list[Path]:
    """生成一组图文卡片：1 张封面 + N 张正文分页（首页图 + 内容图，最多 max_pages 张）。

    之前每篇只出 1 张标题封面，正文全在文案里，发出来是「只有文没有图」的观感。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    body_font = _load_font(52)
    lines = _wrap_cjk(_strip_emoji(body), body_font, CARD_SIZE[0] - 192)
    per_page = 12
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    if len(pages) > max_pages - 1:
        pages = pages[:max_pages - 1]
        pages[-1] = pages[-1][-11:] + ["…（更多内容见正文）"]

    total = len(pages) + 1
    paths = [make_card(title, subtitle, output_dir, platform, footer, page_text=f"1/{total}")]
    for idx, page_lines in enumerate(pages, start=2):
        paths.append(_render_body_page(page_lines, output_dir, platform, footer, f"{idx}/{total}"))
    return paths
