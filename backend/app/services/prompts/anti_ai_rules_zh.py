"""去AI味规则库（中文网文）。Adapted from QMAI (MIT, github.com/Mochocyang/QMAI).

提炼自 QM-QUAI.md：词级/句式黑名单、情绪→动作改写对照、对白原则、
防过度矫正禁令。与 checkers/anti_ai_checker.py 既有词表互补——
本模块只收 QMAI 的具象增量（陈词滥调句式、改写对照），不重复
CHINESE_PROSE_MECHANICS / STYLE_V9 已覆盖的原则宣言。

消费方：
- checkers/anti_ai_checker.py 将 AI_PHRASE_BLACKLIST 合并进短语检测源；
- prose_quality_rules.render_prose_quality_prompt() 追加
  render_anti_ai_prompt_block() 注入生成/润色 prompt。
"""

from __future__ import annotations

# 词级/句式黑名单。按 QM-QUAI 分类组织；条目须为可整串匹配的具象词句
# （供 anti_ai_checker 子串计数），不收"复杂/温柔"这类正常单词。
# 类别按高频优先排序——预算截断时排前的类别先保留。
AI_PHRASE_BLACKLIST: dict[str, list[str]] = {
    "陈词滥调开头": [
        "命运的齿轮开始转动",
        "夜色如墨",
        "空气中弥漫着压抑的气息",
        "这一刻，所有人都沉默了",
        "心中涌起一股",
        "人生会在这一天彻底改变",
    ],
    "机械小动作": [
        "微微一愣",
        "眼中闪过一丝",
        "轻轻点头",
        "缓缓开口",
        "嘴角勾起一抹",
    ],
    "情绪直陈": [
        "感到无比愤怒",
        "充满了悲伤",
        "陷入了深深的自责",
        "终于释然了",
        "心里一暖",
        "复杂的情绪",
    ],
    "万能形容词与泛化词": [
        "无法言喻",
        "难以置信",
        "前所未有",
        "深深地",
        "彻底地",
        "仿佛整个世界",
    ],
    "套路气氛与类型陈词": [
        "空气突然安静下来",
        "气氛暧昧到了极点",
        "心跳不由自主地加快",
        "眼神深情而炽热",
        "阴谋正在浮出水面",
        "真相远比他们想象的更加可怕",
        "天地为之变色",
        "恐怖如斯",
        "一股强大的气息",
        "觉醒了真正的力量",
        "心尖一颤",
    ],
    "解释总结腔": [
        "他终于明白",
        "真正重要的不是",
        "一直以来都错了",
        "不能再逃避了",
        "必须勇敢面对",
    ],
    "无功能景物": [
        "月光洒在大地上",
        "披上了一层银纱",
    ],
}

# 情绪→动作改写对照（QM-QUAI 原文示例，情绪从动作里漏出来，不直陈）。
EMOTION_TO_ACTION_EXAMPLES: list[dict[str, str]] = [
    {
        "bad": "她听到这句话，心里充满了悲伤。",
        "good": "她没接话，只把杯子往自己这边挪了挪。杯底在桌面上蹭出一声轻响。过了好一会儿，她才低声说：“你早就想好了吧？”",
    },
    {
        "bad": "他看着她，眼中闪过一丝复杂的情绪，随后深吸一口气，缓缓开口说道。",
        "good": "他看了她一眼。话到嘴边，又咽了回去。“没事。”他说。",
    },
    {
        "bad": "“我很生气，因为你欺骗了我。我曾经那么信任你，但你却做出了这样的事情。”",
        "good": "“你别说了。”她笑了一下，声音却发紧。“我现在一听你解释，就觉得自己特别蠢。”",
    },
    {
        "bad": "她终于崩溃了，愤怒地质问他为什么要背叛自己。",
        "good": "她点了点头，把桌上的钥匙一枚枚取下来。家门的，车库的，他母亲家的。最后一枚取不下来，指甲劈了一点。他伸手想帮忙。“别碰。”她说。",
    },
    {
        "bad": "他终于明白，真正重要的不是胜利，而是一路上陪伴自己的人。",
        "good": "他看着那枚碎掉的奖牌，忽然笑了一下。“算了。”他说，“回家吃饭吧。”",
    },
]

# 对白改写原则（QM-QUAI「对白润色规则」十条）。
DIALOGUE_PRINCIPLES: list[str] = [
    "能短就短",
    "能含蓄就不要全说破",
    "能用反应表达就不解释",
    "熟人之间少说完整背景",
    "情绪越强，语言越可能破碎",
    "权力关系会改变说话方式",
    "亲密关系会制造省略",
    "隐瞒会制造绕弯",
    "愤怒不总是吼叫",
    "悲伤不总是哭",
]

# 防过度矫正禁令（QM-QUAI「禁止事项」+「允许文字有毛边」）。
ANTI_OVERCORRECTION: list[str] = [
    "禁止把所有文风改成统一的文学腔",
    "禁止把爽文改得过于文艺",
    "禁止过度堆砌华丽辞藻",
    "禁止把口语对白改得像书面演讲",
    "禁止替读者总结主题",
    "禁止为去AI味故意制造错别字或病句",
    "禁止用大量网络热梗破坏原文气质",
    "禁止把含蓄情绪全部明说",
    "禁止把原本简洁有力的句子改复杂",
    "禁止用高级词替换冒充润色",
    "允许文字有毛边：轻微重复、半截话、突然转念、语气词、短句、留白都可保留",
]


def render_anti_ai_prompt_block(max_chars: int = 1800) -> str:
    """渲染注入生成/润色 prompt 的紧凑规则块。

    预算内截断：按行装入，超预算的行跳过。行序即优先级——
    黑名单（高频类别在前）> 改写对照 > 对白原则 > 防过度矫正。
    """
    lines: list[str] = [
        "【anti_ai_phrase_blacklist｜去AI味规则（源自 QMAI）】",
        "以下AI腔标志词句正文禁用；出现时改写为具体动作、物件与对白：",
    ]
    for category, phrases in AI_PHRASE_BLACKLIST.items():
        lines.append(f"- {category}：" + "／".join(phrases))
    lines.append("【情绪→动作改写对照】情绪不直陈，让它从动作和细节里漏出来：")
    for example in EMOTION_TO_ACTION_EXAMPLES:
        lines.append(f"- 坏例：{example['bad']}")
        lines.append(f"  好例：{example['good']}")
    lines.append("【对白原则】" + "；".join(DIALOGUE_PRINCIPLES) + "。")
    lines.append("【防过度矫正】" + "；".join(ANTI_OVERCORRECTION) + "。")

    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)  # +1 for the joining newline
        if used + cost > max_chars:
            continue
        out.append(line)
        used += cost
    return "\n".join(out)
