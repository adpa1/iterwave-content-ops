"""
UltimateGoalChecker — 终极目标检查器（§12.3）。

R2 红线：所有文案必须经 check_content() 后才能进图片卡片制作。
主流程是 G → G2 → H（串行），不是 G → G2 和 G → H 并行。
检查器调用失败或输出无法解析 → 不能默认通过。

三个入口：
- check_content()  → 内容方向性检查（文案生成后）
- check_strategy() → 策略方向检查（复盘调整策略时）
- check_comment()  → 评论合规检查（运营动作前）

G2 质量门三分支：
- 红线命中（redline_keywords）→ 丢弃重生成
- 原则违反（persona_keywords 不匹配）→ 标记修改重检
- 通过 → 进 H（图片卡片制作）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# =====================================================================
# 结果枚举
# =====================================================================

class CheckResult(str, Enum):
    PASS = "pass"
    WARN = "warn"       # 原则违反，建议修改
    FAIL = "fail"       # 红线命中，必须重生成
    ERROR = "error"     # 检查器异常，不能默认通过！


@dataclass
class CheckOutcome:
    """检查结果。"""
    result: CheckResult
    reason: str                          # 失败/警告的原因
    redline_hits: list[str] = None       # 红线关键词命中列表
    persona_matches: list[str] = None     # 画像关键词匹配列表

    def __post_init__(self):
        if self.redline_hits is None:
            self.redline_hits = []
        if self.persona_matches is None:
            self.persona_matches = []

    @property
    def passes(self) -> bool:
        """仅 PASS 算通过。WARN（原则违反、画像不匹配）不算通过 ——
        R2 红线要求"原则违反 → 修改后重检"，WARN 必须进入 G1 重写循环。
        """
        return self.result == CheckResult.PASS

    @property
    def must_regenerate(self) -> bool:
        """红线命中 → 丢弃重生成。"""
        return self.result == CheckResult.FAIL


# =====================================================================
# UltimateGoalChecker
# =====================================================================

class UltimateGoalChecker:
    """终极目标检查器。"""

    def __init__(self, cfg):
        self.cfg = cfg

    # 画像每次实时读取（在线改配置后立即生效），并按平台解析生效画像
    def _persona(self, platform: Optional[str]) -> dict:
        from ..persona import effective_persona
        return effective_persona(self.cfg, platform)

    @property
    def persona_keywords(self) -> list[str]:
        return list(self.cfg.ultimate_goal.persona_keywords)

    @property
    def redline_keywords(self) -> list[str]:
        return list(self.cfg.ultimate_goal.redline_keywords)

    @property
    def forbidden_directions(self) -> list[str]:
        return list(self.cfg.ultimate_goal.forbidden_directions)

    # ------------------------------------------------------------------
    # check_content — 文案方向性检查（G2 质量门入口）
    # ------------------------------------------------------------------

    def check_content(
        self, title: str, body: str, tags: Optional[list[str]] = None,
        platform: Optional[str] = None,
    ) -> CheckOutcome:
        """检查单篇文案内容。

        检查顺序：红线 → 画像匹配 → 禁止方向。
        R2：任何 FAIL → 丢弃重生成；WARN → 修改后重检；ERROR → 不能默认通过。
        """
        text = f"{title} {body} {' '.join(tags or [])}"
        persona = self._persona(platform)
        persona_keywords = persona["persona_keywords"]
        forbidden_directions = persona["forbidden_directions"]

        # 1. 红线命中（最优先级；全局红线 ∪ 平台额外红线）
        redline_hits = self._find_keywords(text, persona["redline_keywords"])
        if redline_hits:
            logger.warning("🚨 终极目标红线命中: %s", redline_hits)
            return CheckOutcome(
                result=CheckResult.FAIL,
                reason=f"内容命中红线关键词: {', '.join(redline_hits)}",
                redline_hits=redline_hits,
            )

        # 2. 画像匹配（原则检查）
        persona_matches = self._find_keywords(text, persona_keywords)
        if persona_keywords and not persona_matches:
            logger.info("⚠️ 文案与账号画像关键词不匹配，需修改后重检")
            return CheckOutcome(
                result=CheckResult.WARN,
                reason="文案与账号画像关键词不匹配，建议加入定位关键词",
                persona_matches=persona_matches,
            )

        # 3. 禁止方向（模糊匹配）
        for direction in forbidden_directions:
            if any(kw in text for kw in direction.split()):
                logger.warning("🚫 文案属于禁止方向: %s", direction)
                return CheckOutcome(
                    result=CheckResult.FAIL,
                    reason=f"文案方向禁止: {direction}",
                )

        return CheckOutcome(
            result=CheckResult.PASS,
            reason="通过终极目标检查",
            persona_matches=persona_matches,
        )

    # ------------------------------------------------------------------
    # check_strategy — 策略方向检查
    # ------------------------------------------------------------------

    def check_strategy(self, strategy: dict) -> CheckOutcome:
        """复盘后策略调整的方向检查。"""
        text = " ".join(str(v) for v in strategy.values() if v)
        redline_hits = self._find_keywords(text, self.redline_keywords)
        if redline_hits:
            return CheckOutcome(
                result=CheckResult.FAIL,
                reason=f"策略方向命中红线: {', '.join(redline_hits)}",
                redline_hits=redline_hits,
            )
        return CheckOutcome(result=CheckResult.PASS, reason="策略通过方向检查")

    # ------------------------------------------------------------------
    # check_comment — 评论合规检查
    # ------------------------------------------------------------------

    def check_comment(self, content: str, platform: Optional[str] = None) -> CheckOutcome:
        """运营动作（评论）前的合规检查。"""
        redline_hits = self._find_keywords(content, self._persona(platform)["redline_keywords"])
        if redline_hits:
            return CheckOutcome(
                result=CheckResult.FAIL,
                reason=f"评论命中红线: {', '.join(redline_hits)}",
                redline_hits=redline_hits,
            )
        # 评论特殊规则：不能留联系方式（微信号 / QQ 号）
        if re.search(r"(微信|wx|vx|wechat)\s*[:：]?\s*\w{5,}", content, re.IGNORECASE):
            return CheckOutcome(
                result=CheckResult.FAIL,
                reason="评论中包含联系方式，违反平台合规",
            )
        if re.search(r"(加QQ|QQ号|QQ\s*[:：]?\s*\d{5,})", content):
            return CheckOutcome(
                result=CheckResult.FAIL,
                reason="评论中包含 QQ 联系方式",
            )
        return CheckOutcome(result=CheckResult.PASS, reason="评论合规")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _find_keywords(text: str, keywords: list[str]) -> list[str]:
        text_lower = text.lower()
        return [kw for kw in keywords if kw.lower() in text_lower]
