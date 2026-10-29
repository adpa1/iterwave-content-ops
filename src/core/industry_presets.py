"""行业模板 — 一键填入画像示例 / 建议维度 / 合规红线（去垂类化）。

所有模板都只是默认值，可随意修改；合规红线是在全局红线之上追加，不替换。
财经 / Web3 / 健康等垂类额外给出平台限制提示（页面展示，不写成硬编码规则）。
"""

from __future__ import annotations

from typing import Optional

PRESETS: list[dict] = [
    {
        "key": "general",
        "name": "通用",
        "description": "不绑定具体行业，适合起步或账号方向还在探索",
        "positioning": "分享真实经验与实用方法，帮读者少走弯路",
        "persona_keywords": ["个人成长", "实用经验", "生活方式", "效率方法"],
        "forbidden_directions": ["成人内容", "纯情绪宣泄无信息量"],
        "extra_redlines": [],
        "dimensions": ["经验方法", "工具测评", "生活方式", "避坑指南"],
        "platform_notes": {},
    },
    {
        "key": "knowledge",
        "name": "知识科普",
        "description": "把复杂知识讲清楚，强调信息密度与可验证来源",
        "positioning": "把复杂知识讲成可执行的方法，强调来源与边界",
        "persona_keywords": ["知识科普", "学习方法", "原理解释", "信息素养"],
        "forbidden_directions": ["伪科学", "无来源的绝对化结论"],
        "extra_redlines": ["包治百病", "祖传秘方"],
        "dimensions": ["原理拆解", "学习方法", "数据解读", "常见误区"],
        "platform_notes": {
            "xhs": "引用数据要标来源，避免「最/第一/绝对」等广告法绝对化用语",
            "douyin": "长解释拆成短句，避免整段念稿",
        },
    },
    {
        "key": "lifestyle",
        "name": "生活方式",
        "description": "日常经验、好物与场景化分享，强调真实体验",
        "positioning": "记录真实生活场景，分享可复用的经验",
        "persona_keywords": ["生活方式", "好物分享", "场景经验", "生活技巧"],
        "forbidden_directions": ["虚假对比", "极端节食", "制造容貌焦虑"],
        "extra_redlines": ["全网最低", "百分百有效"],
        "dimensions": ["好物实测", "生活技巧", "场景改造", "消费避坑"],
        "platform_notes": {
            "xhs": "商业合作需按平台规则报备，避免未标注的软广",
        },
    },
    {
        "key": "finance",
        "name": "财经",
        "description": "财经内容合规要求高，禁止荐股与收益承诺",
        "positioning": "用普通人能听懂的话解释财经现象，不做投资建议",
        "persona_keywords": ["财经解读", "宏观经济", "理财常识", "商业观察"],
        "forbidden_directions": ["荐股", "喊单", "承诺收益", "诱导开户"],
        "extra_redlines": ["稳赚", "保本保收益", "内部消息", "带你炒股", "抄底必涨"],
        "dimensions": ["政策解读", "行业观察", "理财常识", "风险提示"],
        "platform_notes": {
            "xhs": "不得出现具体买卖点位、荐股、收益承诺；涉及个股只做现象讨论",
            "douyin": "财经类内容需避免「带单」「跟投」表述，不引导站外交易",
            "kuaishou": "涉及金融产品需资质，普通账号不做产品推荐",
            "wechat_channels": "不发布未经核实的市场传闻，标注信息仅为科普",
        },
    },
    {
        "key": "web3",
        "name": "Web3",
        "description": "链上内容高风险，禁止代币推荐与站外引流",
        "positioning": "解释链上机制与风险，不推荐任何代币或项目",
        "persona_keywords": ["区块链", "Web3", "链上数据", "加密科普"],
        "forbidden_directions": ["代币推荐", "合约带单", "空投返佣", "站外引流"],
        "extra_redlines": ["百倍币", "稳赚空投", "带你上车", "私域带单", "合约必胜"],
        "dimensions": ["机制科普", "链上数据", "风险案例", "常见骗局"],
        "platform_notes": {
            "xhs": "禁止代币名称+收益暗示组合，禁止引导加群/私信索取链接",
            "douyin": "不出现交易平台邀请码、返佣链接、带单话术",
            "kuaishou": "不发布投资回报截图，不承诺收益",
            "wechat_channels": "不引导站外社群，涉及项目只做风险讨论",
        },
    },
    {
        "key": "health",
        "name": "健康",
        "description": "健康内容禁止疗效承诺与处方建议，强调就医边界",
        "positioning": "分享可验证的健康常识，明确不替代医生诊断",
        "persona_keywords": ["健康科普", "运动习惯", "饮食结构", "睡眠管理"],
        "forbidden_directions": ["医疗诊断", "处方药推荐", "疗效保证", "替代就医"],
        "extra_redlines": ["包治", "根治", "药到病除", "无需就医", "替代药物"],
        "dimensions": ["健康常识", "运动方法", "饮食搭配", "误区澄清"],
        "platform_notes": {
            "xhs": "不得出现具体药品名称+疗效承诺；身体问题建议就医",
            "douyin": "不做诊断式表述（「你这是××病」），不推荐处方药",
            "kuaishou": "不发布前后对比暗示疗效的图片",
        },
    },
]


def list_presets() -> list[dict]:
    return PRESETS


def get_preset(key: str) -> Optional[dict]:
    return next((p for p in PRESETS if p["key"] == key), None)
