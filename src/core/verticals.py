"""行业模板：帮新用户快速填好画像、选题维度和合规红线。

平台本身不绑定任何垂类。模板只是「起点」：套用后填进表单，用户随时改；
不会自动覆盖已保存的配置。红线词以「追加」方式合并，不删除已有词。

每个模板：
  positioning_example : 一句话定位示例（只作占位提示）
  persona_keywords    : 定位关键词
  forbidden_directions: 禁止方向
  redline_keywords    : 该行业额外的合规红线词（追加到全局红线）
  dimensions          : 建议的选题维度（选题库没有维度列时可参考）
  hot_sources         : 预留：建议的行业热点来源（RSS），经核实后再填
  compliance_note     : 该行业在国内内容平台的常见限制，展示给用户
"""

from __future__ import annotations

from typing import Any

VERTICALS: list[dict[str, Any]] = [
    {
        "id": "general",
        "name": "通用",
        "positioning_example": "用简单的话讲清楚一件事的分享者",
        "persona_keywords": [],
        "forbidden_directions": ["不做低俗、引战内容", "不搬运未授权内容"],
        "redline_keywords": [],
        "dimensions": ["干货", "观点", "经验", "清单"],
        "hot_sources": [],
        "compliance_note": "",
    },
    {
        "id": "knowledge",
        "name": "知识科普 / 教育",
        "positioning_example": "把复杂知识讲成三分钟能听懂的科普号",
        "persona_keywords": ["科普", "知识", "学习方法", "冷知识"],
        "forbidden_directions": ["不做未经证实的结论", "不贩卖焦虑"],
        "redline_keywords": ["包过", "保过", "押题", "代考"],
        "dimensions": ["概念解释", "学习方法", "常见误区", "工具推荐"],
        "hot_sources": [],
        "compliance_note": "教育类避免承诺考试结果、代考代写；涉及未成年人内容需格外谨慎。",
    },
    {
        "id": "lifestyle",
        "name": "生活方式 / 视觉内容",
        "positioning_example": "每天分享一张让人心情变好的图",
        "persona_keywords": ["生活", "治愈", "日常", "审美"],
        "forbidden_directions": ["不做低质搬运", "不做与定位无关的泛流量内容"],
        "redline_keywords": [],
        "dimensions": ["日常", "灵感", "合集", "教程"],
        "hot_sources": [],
        "compliance_note": "图片类注意版权：使用自有、可商用或已授权素材。",
    },
    {
        "id": "finance",
        "name": "财经 / 经济",
        "positioning_example": "用普通人听得懂的话解读经济新闻",
        "persona_keywords": ["经济", "财经", "宏观", "政策解读", "理财常识"],
        "forbidden_directions": ["不荐股、不喊单", "不承诺收益", "不做内幕消息", "不引导站外交易"],
        "redline_keywords": ["稳赚", "保本", "翻倍", "内幕", "荐股", "带单", "必涨", "保证收益", "加群领牛股"],
        "dimensions": ["宏观解读", "政策变化", "行业观察", "理财常识", "数据图解"],
        "hot_sources": [],
        "compliance_note": "国内平台对财经内容审核严格：禁止荐股、承诺收益、非法集资引流；"
                           "建议正文加「不构成投资建议」。持牌要求以平台规则为准。",
    },
    {
        "id": "web3",
        "name": "Web3 / 区块链",
        "positioning_example": "讲清楚区块链技术和行业动态的科普号",
        "persona_keywords": ["区块链", "Web3", "技术科普", "行业动态"],
        "forbidden_directions": ["不推荐具体币种", "不做交易引导", "不发空投/拉新链接", "不讨论绕过监管"],
        "redline_keywords": ["空投", "百倍币", "梭哈", "合约带单", "拉盘", "交易所邀请码", "保证收益", "上车", "暴富"],
        "dimensions": ["技术原理", "行业动态", "安全防骗", "政策监管"],
        "hot_sources": [],
        "compliance_note": "国内平台对虚拟货币交易、ICO、交易所引流内容基本禁止，账号容易被限流或封禁；"
                           "建议只做技术科普与防骗，不涉及币价与交易。",
    },
    {
        "id": "health",
        "name": "健康 / 医疗",
        "positioning_example": "用循证知识帮大家少走弯路的健康科普号",
        "persona_keywords": ["健康", "科普", "运动", "饮食"],
        "forbidden_directions": ["不替代医生诊断", "不推荐处方药", "不做偏方"],
        "redline_keywords": ["根治", "包治", "神药", "特效药", "无副作用", "祖传秘方"],
        "dimensions": ["常见误区", "日常习惯", "运动饮食", "就医指南"],
        "hot_sources": [],
        "compliance_note": "医疗健康内容需避免诊疗承诺和夸大功效；部分平台要求医疗类作者资质认证。",
    },
]

_BY_ID = {v["id"]: v for v in VERTICALS}


def list_verticals() -> list[dict[str, Any]]:
    return VERTICALS


def get_vertical(vid: str) -> dict[str, Any] | None:
    return _BY_ID.get(vid)
