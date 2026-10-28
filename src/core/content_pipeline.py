"""
内容生产线 — 文案生成 + 图片卡片制作（§3 / §5）。

R2 红线：本模块只负责 G2 之前的产物生成，**所有文案必须交给
UltimateGoalChecker.check_content() 检查后才能进入卡片制作**（由
RoundExecutor 串行编排，本模块不自行放行）。

LLM 策略：
- config.llm.enabled=true 且环境变量有 key → 调用 Agent Plan（ARK）生成
- 否则 → 模板降级（明确标注 degraded=True，不伪装成 AI 生成）
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


# =====================================================================
# 平台文案风格
# =====================================================================

PLATFORM_STYLES: dict[Platform, dict] = {
    Platform.XHS: {
        "title_style": "emoji + 数字式标题，突出获得感",
        "body_style": "分段清晰，带要点符号，结尾引导互动",
        "hashtag": ["#AI学习", "#效率工具", "#个人成长"],
        "tone": "亲切、第一人称、口语化",
    },
    Platform.DOUYIN: {
        "title_style": "口语化，直接点痛点",
        "body_style": "短段落，节奏快，适合配字幕",
        "hashtag": ["#AI", "#干货分享", "#学习"],
        "tone": "有冲击力、直接",
    },
    Platform.KUAISHOU: {
        "title_style": "接地气，直接给方案",
        "body_style": "直白，不绕弯子",
        "hashtag": ["#AI", "#涨知识"],
        "tone": "朴实、真实",
    },
    Platform.WECHAT_CHANNELS: {
        "title_style": "正式但不生硬，偏观点式",
        "body_style": "结构化，适合长阅读",
        "hashtag": ["#AI教育", "#科技"],
        "tone": "专业、克制",
    },
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
    """生成单平台文案。LLM 可用则用 LLM，否则模板降级。

    返回 (title, body, tags)。任何异常都不抛出，降级到模板，
    保证 G2 检查器一定能拿到内容做判定（R2：不能让空内容溜过去）。

    reject_reason：上一版被 G2 拒收的原因。非空表示这是重生成，
    LLM 会收到改写要求；模板降级无法自我改写，仍会原样返回（由调用方判定重试上限）。

    conn 不为空时，LLM 调用会写 ai_calls 审计表（tokens/latency/success）。
    """
    if cfg is not None and _llm_available(cfg):
        try:
            result = _generate_via_llm(
                cfg, topic_title, topic_summary, dimension, platform,
                conn=conn, topic_id=topic_id, round_id=round_id,
                reject_reason=reject_reason, strategy_hints=strategy_hints,
            )
            if result:
                return result
        except Exception:  # noqa: BLE001 — LLM 失败必须降级而不是中断
            logger.exception("LLM 文案生成失败，降级为模板 platform=%s", platform.value)

    return _generate_via_template(topic_title, topic_summary, dimension, platform)


def build_hint_block(strategy_hints: Optional[list[str]]) -> str:
    """上一轮复盘要点 → 提示词片段。要点已在 autogrow 里过了合规方向检查；
    这里只作参考，明确不得为迎合数据脱离方向或触碰红线。"""
    hints = [h.strip() for h in (strategy_hints or []) if h and h.strip()][:6]
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
    conn=None, topic_id: Optional[str] = None, round_id: Optional[int] = None,
    reject_reason: Optional[str] = None,
    strategy_hints: Optional[list[str]] = None,
) -> Optional[tuple[str, str, list[str]]]:
    """调用 Agent Plan（ARK）Chat Completions，强制 JSON 输出。"""
    import httpx

    llm = cfg.llm
    style = PLATFORM_STYLES[platform]
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
        f"摘要：{topic_summary[:300]}\n"
        f"内容方向：{dimension}\n"
        f"要求：标题不超过 20 字；正文 150-400 字；标签 3-5 个；"
        f"不得出现违法违规内容；不得脱离「{dimension}」方向。"
    )
    prompt_hints = build_hint_block(strategy_hints)
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
    except Exception as exc:
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens_in=None, tokens_out=None, success=False, output_valid=False,
        )
        raise exc
    finally:
        if client is not None:
            client.close()

    latency_ms = int((time.monotonic() - started) * 1000)
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    tokens_in = usage.get("prompt_tokens")
    tokens_out = usage.get("completion_tokens")

    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        _log_ai_call(
            conn, platform, llm.model, topic_id, round_id,
            latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
            success=True, output_valid=False,
        )
        logger.warning("LLM 输出非 JSON，降级模板")
        return None
    title = str(parsed.get("title") or "").strip()
    body = str(parsed.get("body") or "").strip()
    tags = parsed.get("tags") or style["hashtag"]
    output_valid = bool(title and body)
    _log_ai_call(
        conn, platform, llm.model, topic_id, round_id,
        latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
        success=True, output_valid=output_valid,
    )

    if not output_valid:
        logger.warning("LLM 返回字段不完整，降级模板")
        return None
    if not isinstance(tags, list):
        tags = style["hashtag"]
    return title[:40], body, [str(t) for t in tags][:6]


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
) -> tuple[str, str, list[str]]:
    """模板降级（无 LLM 时使用，质量低于 LLM 但结构完整）。"""
    style = PLATFORM_STYLES[platform]
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

    return title, body, list(style["hashtag"])


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
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)


def _load_font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
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
    footer: str = "AI 教育 · 每日一招",
) -> Path:
    """生成 1080×1440 图文卡片（渐变背景 + 折行标题 + 维度标签 + 页脚）。

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
    tag_text = f"＃{subtitle}" if subtitle else "＃知识卡片"
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

    # 页脚
    footer_font = _load_font(36)
    draw.text(
        (margin, height - margin - 60), footer,
        fill=(255, 255, 255), font=footer_font,
    )

    img.save(str(output_path), format="PNG", optimize=True)
    logger.info("🖼️ 图片卡片生成: %s (%dx%d)", output_path, width, height)
    return output_path
