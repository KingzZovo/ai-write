"""Genre-specific writing rules library — 40+ genre templates.

Each genre template provides:
- Core rules (what makes this genre work)
- Pacing guidance (rhythm patterns)
- Taboos (things to avoid)
- Hook patterns (genre-specific hooks)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GenreTemplate:
    name: str
    label: str  # Chinese display name
    rules: list[str] = field(default_factory=list)
    pacing: str = ""
    taboos: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


GENRE_TEMPLATES: dict[str, GenreTemplate] = {
    "xuanhuan": GenreTemplate(
        name="xuanhuan", label="玄幻",
        rules=["等级体系清晰且有递进感", "战斗描写注重招式细节和力量对比", "修炼突破要有铺垫和仪式感", "势力格局层层递进", "主角金手指要合理化"],
        pacing="前期快速升级建立爽感，中期放缓扩展世界观，后期高潮迭起",
        taboos=["等级体系混乱", "无脑碾压无挑战", "修炼突破过于轻松", "忽视配角塑造"],
        hooks=["实力暴露", "宝物出世", "越级挑战", "身份揭露", "禁地探索"],
        keywords=["修炼", "突破", "灵气", "功法", "秘境"],
    ),
    "xianxia": GenreTemplate(
        name="xianxia", label="仙侠",
        rules=["道法自然的哲学内核", "仙凡有别的等级压制", "劫难与心魔的双重考验", "门派体系和师徒传承", "天材地宝和丹道炼器"],
        pacing="慢热铺垫→渐入佳境→大劫来临→浴火重生→飞升大结局",
        taboos=["轻易飞升无感", "无脑杀伐失去仙气", "心性不符合修道"],
        hooks=["天劫降临", "大能转世", "远古遗迹", "飞升契机", "因果纠缠"],
        keywords=["飞升", "渡劫", "道心", "天道", "因果"],
    ),
    "dushi": GenreTemplate(
        name="dushi", label="都市",
        rules=["贴近现实生活逻辑", "职场/商战/社交的合理描写", "情感线要有化学反应", "金手指不能太离谱", "装逼打脸节奏明快"],
        pacing="小目标→初步成功→遇挫→更大成功→终极目标",
        taboos=["脱离社会常识", "女角色工具化", "装逼过度无底线"],
        hooks=["身份反转", "商业博弈", "过去的秘密", "新的对手出现"],
        keywords=["商战", "逆袭", "豪门", "重生", "职场"],
    ),
    "yanqing": GenreTemplate(
        name="yanqing", label="言情",
        rules=["男女主角化学反应是核心", "误会冲突要合理不做作", "感情线渐进式发展", "配角不能喧宾夺主", "甜虐比例要控制"],
        pacing="相遇→互有好感→阻碍→确认心意→大虐→HE",
        taboos=["感情线突兀", "三观不正的恋爱", "配角比主角精彩", "虐无极限"],
        hooks=["破镜重圆", "身份秘密", "情敌出现", "过去的创伤"],
        keywords=["甜宠", "虐恋", "契约", "重逢", "暗恋"],
    ),
    "xuanyi": GenreTemplate(
        name="xuanyi", label="悬疑",
        rules=["线索布局公平合理", "节奏紧凑环环相扣", "反转要有前置伏笔", "氛围营造是关键", "逻辑链条完整"],
        pacing="悬念开场→调查推进→假答案→真相反转→案件收束",
        taboos=["凶手无前置线索", "超自然解释破坏逻辑", "节奏拖沓", "无意义的恐怖描写"],
        hooks=["新线索出现", "嫌疑人变化", "不在场证明崩塌", "过去的案件关联"],
        keywords=["真相", "线索", "推理", "密室", "动机"],
    ),
    "kehuan": GenreTemplate(
        name="kehuan", label="科幻",
        rules=["科学设定内部自洽", "技术发展的社会影响", "硬科幻注重物理合理性", "软科幻注重人文思考", "未来社会形态要有深度"],
        pacing="世界观引入→冲突建立→技术挑战→哲学思辨→宏大结局",
        taboos=["科学设定自相矛盾", "技术万能化", "忽视人文关怀"],
        hooks=["技术突破", "外星接触", "AI觉醒", "时间悖论", "文明冲突"],
        keywords=["星际", "AI", "时空", "文明", "进化"],
    ),
    "lishi": GenreTemplate(
        name="lishi", label="历史",
        rules=["尊重基本历史框架", "人物言行符合时代特征", "避免现代思维穿越", "历史细节考据准确", "虚构部分要合理"],
        pacing="时代背景交代→卷入历史洪流→建功立业→历史转折→结局",
        taboos=["严重违背史实", "用现代价值观评判古人", "称呼用错"],
        hooks=["历史事件参与", "权谋博弈", "战争转折", "身份之谜"],
        keywords=["朝堂", "战场", "权谋", "乱世", "英雄"],
    ),
    "moshi": GenreTemplate(
        name="moshi", label="末世",
        rules=["生存压力要真实", "人性考验是核心主题", "资源争夺的残酷性", "势力博弈和基地建设", "异变/丧尸设定要自洽"],
        pacing="灾变发生→求生挣扎→建立据点→势力冲突→终极危机",
        taboos=["末世当种田文写", "主角无敌无压力", "忽视生存逻辑"],
        hooks=["基地危机", "变异进化", "物资短缺", "背叛", "新威胁"],
        keywords=["丧尸", "辐射", "基地", "进化", "末日"],
    ),
    "wuxia": GenreTemplate(
        name="wuxia", label="武侠",
        rules=["江湖义气为核心", "武功招式要有画面感", "恩怨情仇层层递进", "门派纷争的江湖格局", "侠之大者为国为民"],
        pacing="初入江湖→学艺成长→卷入纷争→揭开真相→终极对决",
        taboos=["武功描写空洞", "忽视武德和规矩", "缺少江湖味"],
        hooks=["武林秘籍", "仇家追杀", "身世之谜", "武林大会"],
        keywords=["江湖", "剑法", "内力", "门派", "恩怨"],
    ),
    "guyan": GenreTemplate(
        name="guyan", label="古言",
        rules=["礼教约束下的感情突破", "宅斗宫斗的策略性", "女主成长线要完整", "古代礼仪和称呼准确", "服饰饮食等细节真实"],
        pacing="入府/入宫→站稳脚跟→暗斗升级→逆袭→大结局",
        taboos=["现代用语混入", "女主圣母无脑", "宅斗毫无逻辑"],
        hooks=["身份揭露", "联姻阴谋", "嫡庶之争", "前世今生"],
        keywords=["嫡女", "王妃", "后宫", "世家", "权谋"],
    ),
    "xitong": GenreTemplate(
        name="xitong", label="系统流",
        rules=["系统规则明确不随意改变", "任务设计有趣有挑战", "奖惩机制合理", "系统不能万能", "主角要有自主性不只是系统傀儡"],
        pacing="获得系统→熟悉规则→完成任务升级→系统升级→终极任务",
        taboos=["系统规则随意更改", "奖励过于丰厚失去紧张感", "主角完全被系统控制"],
        hooks=["隐藏任务", "系统升级", "惩罚机制触发", "系统来源之谜"],
        keywords=["系统", "任务", "积分", "升级", "抽奖"],
    ),
    "chuanyue": GenreTemplate(
        name="chuanyue", label="穿越",
        rules=["穿越者的知识优势要合理利用", "适应异世界的过程要真实", "不要碾压土著智商", "文化冲突是看点", "现代知识的应用要考虑时代背景"],
        pacing="穿越→适应→利用知识建立优势→面对原住民挑战→站稳脚跟",
        taboos=["穿越后立刻无敌", "土著全是蠢货", "现代知识无限制使用"],
        hooks=["身份暴露危机", "时空裂缝", "回到现代的可能", "同穿越者"],
        keywords=["穿越", "异世界", "金手指", "蝴蝶效应"],
    ),
    "chongsheng": GenreTemplate(
        name="chongsheng", label="重生",
        rules=["前世记忆是最大金手指", "改变命运的合理性", "蝴蝶效应导致未知变化", "不能全知全能", "情感纠葛要有新发展"],
        pacing="重生回到过去→弥补遗憾→改变关键节点→蝴蝶效应→新结局",
        taboos=["完全按前世剧本走", "所有人都是NPC", "重生后性格不变"],
        hooks=["前世仇人提前出现", "蝴蝶效应意外", "记忆模糊的关键信息"],
        keywords=["重生", "前世", "逆袭", "弥补", "改变"],
    ),
}


def get_genre_template(genre: str) -> GenreTemplate | None:
    """Get a genre template by name or label."""
    if genre in GENRE_TEMPLATES:
        return GENRE_TEMPLATES[genre]
    for t in GENRE_TEMPLATES.values():
        if t.label == genre:
            return t
    return None


def get_all_genres() -> list[dict]:
    """Get all genre templates as dicts for API response."""
    return [
        {"name": t.name, "label": t.label, "rule_count": len(t.rules),
         "keywords": t.keywords}
        for t in GENRE_TEMPLATES.values()
    ]


def compile_genre_prompt(genre: str) -> str:
    """Compile genre-specific rules into a prompt instruction string."""
    template = get_genre_template(genre)
    if not template:
        return ""

    parts = [f"【题材：{template.label}】"]
    if template.rules:
        parts.append("\n【写作规则】")
        for r in template.rules:
            parts.append(f"- {r}")
    if template.pacing:
        parts.append(f"\n【节奏指导】{template.pacing}")
    if template.taboos:
        parts.append("\n【禁忌事项】")
        for t in template.taboos:
            parts.append(f"- 避免：{t}")
    if template.hooks:
        parts.append("\n【推荐钩子】" + "、".join(template.hooks))
    return "\n".join(parts)
