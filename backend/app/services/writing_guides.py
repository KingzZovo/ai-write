"""
Writing Guide Engine

Comprehensive writing guidance system for Chinese web novel generation.

Modules:
- WRITING_MODULES: 7 core writing technique modules
- WRITING_PROHIBITIONS: Common mistakes to avoid
- HOOK_TECHNIQUES: 13 chapter hook types
- CHAPTER_STRUCTURE: Standard chapter structure template
- GENRE_TEMPLATES: 10+ genre-specific templates
- AI_WORD_BLACKLIST: Words that betray AI authorship
- build_writing_prompt(): Main entry point for prompt assembly
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# 7 Core Writing Modules
# =====================================================================

WRITING_MODULES: dict[str, dict[str, Any]] = {
    "show_not_tell": {
        "name": "展示而非讲述",
        "description": "通过动作、对话、感官细节展示信息，而非直接告诉读者",
        "rules": [
            "禁止使用'他感到很悲伤'，改为描写他的动作和表情",
            "禁止使用'她很美丽'，改为描写具体特征和他人反应",
            "情绪通过身体反应展示：手抖、呼吸加速、瞳孔收缩",
            "性格通过选择展示：面对困境时的决策揭示人物本质",
            "关系通过互动展示：对话方式、距离、肢体语言",
        ],
        "examples": [
            "差: 他非常愤怒。",
            "好: 他的指节捏得发白，青筋从手背一路蔓延到小臂。",
            "差: 她是一个善良的人。",
            "好: 她把最后一块干粮塞进孩子手里，自己转身咽了口唾沫。",
        ],
    },
    "scene_immersion": {
        "name": "场景沉浸感",
        "description": "调动五感+动态描写，让读者身临其境",
        "rules": [
            "每个重要场景至少调动3种感官(视/听/嗅/触/味)",
            "静态场景用一两个动态细节打破：风吹帘动、烛火摇曳",
            "用角色视角限制信息，增加代入感",
            "环境描写为情节服务：压抑场景用暗色调，紧张场景用声响",
            "避免上帝视角平铺直叙，通过角色的眼睛去观察世界",
        ],
        "examples": [
            "差: 这是一个很大的山洞，里面很暗。",
            "好: 火折子嗤地亮了，橘红色的光舔上湿漉漉的石壁，水珠折射出细碎的光斑。脚下传来咕咕的暗河声，风从深处灌来，带着一股铁锈般的腥甜。",
        ],
    },
    "dialogue_craft": {
        "name": "对话工艺",
        "description": "对话要区分人物、推进情节、传递信息",
        "rules": [
            "每个角色的说话方式应有辨识度：用词、句长、口头禅",
            "对话应有潜台词：角色说的和想的不一定一致",
            "避免'问答乒乓球'：穿插动作、心理、环境描写",
            "信息通过冲突性对话传递，而非角色互相解释背景",
            "重要对话前后加动作线(action beat)代替'XX说'",
        ],
        "examples": [
            "差: '你好。''你好。''最近怎么样？''还好。'",
            "好: 他摩挲着杯沿，目光落在窗外。'听说北边出事了。'\n她搁下筷子的手顿了一下。'哪个北边？'\n'你知道我说的是哪个。'",
        ],
    },
    "tension_control": {
        "name": "张力控制",
        "description": "通过节奏、句式、信息控制叙事张力",
        "rules": [
            "高张力场景：短句、省略主语、动作密集、删除形容词",
            "低张力过渡：长句、环境描写、心理活动、舒缓节奏",
            "信息不对称制造张力：读者知道但角色不知道(反讽)",
            "倒计时效应：明确的截止时间/距离增加紧迫感",
            "延迟满足：在揭晓答案前多加一个转折",
        ],
        "examples": [
            "高张力: 刀来了。他侧身。慢了半步。血线从肋间绽开。",
            "低张力: 午后的阳光从窗缝挤进来，在地板上画出一道金色的梯形。他靠着墙根坐了很久，直到影子爬过他的膝盖。",
        ],
    },
    "micro_tension": {
        "name": "微观张力",
        "description": "每段话、每个句子都包含读者继续阅读的理由",
        "rules": [
            "每段结尾制造微型悬念或引出下一段",
            "避免完全闭合的段落：留一个开口引向下文",
            "利用'but/therefore'替代'and then'推进",
            "每500字至少一个信息钩子(问题/冲突/发现)",
            "删除任何不推进情节或不深化人物的段落",
        ],
        "examples": [
            "差: 他吃完饭，然后去睡觉了。(And then - 无张力)",
            "好: 他强迫自己咽下最后一口饭——但刚放下筷子，门外的脚步声就停了。(But - 有张力)",
        ],
    },
    "emotional_resonance": {
        "name": "情感共鸣",
        "description": "通过具象化和共情技巧触动读者情感",
        "rules": [
            "情感具象化：将抽象情绪转化为具体意象和行为",
            "反差产生共鸣：强者的脆弱、冷酷者的温柔更打动人",
            "利用普世经验：思乡、别离、失去、等待",
            "情感要有铺垫：在爆发前用3-5处暗示积蓄",
            "克制比渲染更有力：不说'他很难过'，让读者自己难过",
        ],
        "examples": [
            "差: 她非常难过，泪如雨下，心如刀割。",
            "好: 她从柜子里翻出那件洗得发白的校服，抖开，对着光看了很久。然后叠好，放回去。",
        ],
    },
    "info_weaving": {
        "name": "信息编织",
        "description": "将世界观、背景、设定自然融入叙事",
        "rules": [
            "信息通过冲突传递：角色因设定规则产生矛盾时自然展示",
            "同一设定不要集中解释，分散到3-5个场景中逐步揭示",
            "用角色的无知/好奇自然引出设定介绍",
            "避免'设定百科'：角色不会在战斗中讲解力量体系",
            "第一次提到设定时点到为止，后续使用时才详细展开",
        ],
        "examples": [
            "差: 在这个世界里，修炼分为九重天，每重天又分初期中期后期......",
            "好: '三重天？'师兄嗤笑一声，'老子十四岁就三重天了，你今年多大？'\n他攥紧了拳头。二十三。",
        ],
    },
}

# =====================================================================
# Writing Prohibitions
# =====================================================================

WRITING_PROHIBITIONS: list[str] = [
    "禁止大段独白解释背景设定(设定百科问题)",
    "禁止角色自我介绍式的心理活动('我是XX，我的能力是...')",
    "禁止无冲突的日常流水账",
    "禁止主角无理由好运(开挂需要代价或伏笔)",
    "禁止配角纸片化(哪怕是反派也要有动机)",
    "禁止用省略号代替实际描写('他......')",
    "禁止同一信息重复表达(作者、角色、旁白各说一遍)",
    "禁止'告诉读者感受'('这让所有人都震惊了')",
    "禁止机械化的战斗描写('A攻击B，B防御，A再攻击')",
    "禁止无铺垫的实力暴涨(升级需要过程)",
    "禁止角色突然变蠢来推动剧情(降智推动)",
    "禁止无意义的拖延和灌水(每段都要有存在意义)",
]

# =====================================================================
# Hook Techniques (13 types)
# =====================================================================

HOOK_TECHNIQUES: dict[str, dict[str, str]] = {
    "悬念钩": {
        "description": "在章节结尾抛出一个未解之谜",
        "example": "门外站着一个人——一个本不该还活着的人。",
        "usage": "适合所有类型章节，尤其是信息揭露章",
    },
    "危机钩": {
        "description": "将角色置于即将面临的危险中",
        "example": "就在这时，空气中传来了那种他最不愿闻到的气味——血。不是别人的，是他自己的。",
        "usage": "适合战斗/冒险章节结尾",
    },
    "反转钩": {
        "description": "在结尾揭示一个颠覆性的事实",
        "example": "'你以为我是谁？'她摘下面具，露出一张他做梦都不敢忘的脸。",
        "usage": "适合关键剧情章节，不宜频繁使用",
    },
    "承诺钩": {
        "description": "角色做出一个影响未来走向的决定",
        "example": "他站起身，最后看了一眼身后的城市。'我会回来的。'但他知道，他不会。",
        "usage": "适合阶段转折点",
    },
    "倒计时钩": {
        "description": "设定明确的时间限制",
        "example": "解药还剩三天的效力。而那个人，至少在五天路程之外。",
        "usage": "适合营造紧迫感",
    },
    "秘密钩": {
        "description": "暗示一个尚未揭开的秘密",
        "example": "老者欲言又止，最终还是咽下了那句话。有些真相，说出来比不说更残忍。",
        "usage": "适合伏笔铺设",
    },
    "情感钩": {
        "description": "触动读者情感的场景",
        "example": "她转过身的时候，他看见她咬着下唇——就像小时候忍着不哭的样子。",
        "usage": "适合感情线推进章节",
    },
    "新角色钩": {
        "description": "引入一个引发好奇的新角色",
        "example": "酒馆角落的黑衣人终于抬起头。在场所有人同时后退了一步。",
        "usage": "适合新弧开始",
    },
    "宝物钩": {
        "description": "展示一个令人垂涎的奖励或发现",
        "example": "箱子打开的瞬间，金色的光芒刺得他眯起了眼。他知道，这东西能让整个江湖翻天。",
        "usage": "适合冒险/寻宝章节",
    },
    "预言钩": {
        "description": "一个关于未来的暗示或预言",
        "example": "'三年之后。'算命先生收起龟壳，'会有一场大雨。你最好学会游泳。'",
        "usage": "适合铺垫长线伏笔",
    },
    "背叛钩": {
        "description": "暗示信任即将被打破",
        "example": "他把背后交给了最信任的兄弟。而他没看见的是，那双手正在摸向腰间的匕首。",
        "usage": "适合关系转折章节",
    },
    "升级钩": {
        "description": "暗示角色即将获得新能力",
        "example": "那个折磨了他三个月的瓶颈——他感觉到了一丝松动。就在丹田深处，有什么东西在苏醒。",
        "usage": "适合修炼/成长章节",
    },
    "世界观钩": {
        "description": "揭示一个颠覆已知世界观的信息",
        "example": "'你以为这里是灵界的底层？'老人摇了摇头，'不，这里是顶层。'",
        "usage": "适合世界观拓展章节",
    },
}

# =====================================================================
# Chapter Structure Template
# =====================================================================

CHAPTER_STRUCTURE: dict[str, str] = {
    "opening": (
        "开篇(前10-15%): 快速切入场景，用对话/动作/悬念开场。"
        "承接上章结尾或制造新的兴趣点。"
        "不要用大段环境描写开场(除非是新卷开始)。"
    ),
    "development": (
        "发展(15-60%): 推进核心事件。穿插冲突、对话、描写。"
        "每500字至少一个信息钩子。注意紧-松-紧的节奏波动。"
        "在25%和50%处各安排一个小高潮或信息揭露。"
    ),
    "climax": (
        "高潮(60-85%): 本章核心冲突/事件的最高点。"
        "使用短句、快节奏。删减修饰词。"
        "让角色做出关键选择或面对最大挑战。"
    ),
    "resolution_hook": (
        "收束+钩子(85-100%): 短暂的喘息或余韵。"
        "最后2-3段埋下钩子，驱动读者翻页。"
        "不要完全闭合——留一个开口给下一章。"
    ),
}

# =====================================================================
# Genre-Specific Templates
# =====================================================================

GENRE_TEMPLATES: dict[str, dict[str, Any]] = {
    "玄幻": {
        "name": "玄幻",
        "core_elements": ["修炼体系", "境界突破", "宗门势力", "法宝灵器", "天材地宝"],
        "pacing": "1-3章日常/修炼 -> 1-2章冲突 -> 1章高潮 -> 1章过渡",
        "key_hooks": ["升级钩", "宝物钩", "危机钩"],
        "prohibitions": [
            "禁止无铺垫的境界突破",
            "禁止战斗只写'他挥出一拳，对方倒飞出去'",
            "禁止修炼段落超过500字纯心理独白",
        ],
        "style_notes": "热血为主，金手指需要代价，升级节奏不宜过快也不宜过慢。",
    },
    "仙侠": {
        "name": "仙侠",
        "core_elements": ["道与法", "劫", "轮回", "天道", "仙凡有别", "因果"],
        "pacing": "修道感悟穿插人情冷暖，大战前有静谧的铺垫",
        "key_hooks": ["悬念钩", "预言钩", "世界观钩"],
        "prohibitions": [
            "禁止纯口水话的论道",
            "禁止降智推动修仙政治剧情",
        ],
        "style_notes": "文笔偏古风，注意用词雅致。天地法则描写要有意境。",
    },
    "都市": {
        "name": "都市",
        "core_elements": ["职场", "商战", "社会关系", "都市异能", "重生/系统"],
        "pacing": "节奏明快，每章有1-2个社交冲突或关键事件",
        "key_hooks": ["反转钩", "秘密钩", "情感钩"],
        "prohibitions": [
            "禁止过度装逼打脸模板化",
            "禁止配角全部崇拜主角",
            "禁止社交场景缺乏真实感",
        ],
        "style_notes": "语言现代化、口语化。对话要有生活感。",
    },
    "言情": {
        "name": "言情",
        "core_elements": ["CP互动", "情感拉扯", "误会与和解", "成长", "甜虐交替"],
        "pacing": "甜3章虐1章的节奏，虐后必有甜。情感线和事业线交替推进",
        "key_hooks": ["情感钩", "秘密钩", "承诺钩"],
        "prohibitions": [
            "禁止无理由吃醋",
            "禁止强制降智制造误会",
            "禁止女主只围着男主转(要有自己的事业线)",
        ],
        "style_notes": "注重细腻的情感描写和小细节。对话要有暧昧感和张力。",
    },
    "悬疑": {
        "name": "悬疑",
        "core_elements": ["谜题", "线索", "推理", "红鲱鱼", "真相揭示"],
        "pacing": "谜题提出 -> 线索收集 -> 假说建立 -> 假说推翻 -> 真相逼近",
        "key_hooks": ["悬念钩", "反转钩", "秘密钩"],
        "prohibitions": [
            "禁止关键线索无铺垫突然出现(公平推理)",
            "禁止真凶是完全没出现过的角色",
            "禁止推理逻辑跳跃",
        ],
        "style_notes": "氛围营造很重要。每章结尾留一个新问题。线索需要公平地展示给读者。",
    },
    "科幻": {
        "name": "科幻",
        "core_elements": ["科技设定", "社会推演", "伦理困境", "未知探索"],
        "pacing": "设定展示穿插人物故事，避免大段硬科普",
        "key_hooks": ["世界观钩", "悬念钩", "危机钩"],
        "prohibitions": [
            "禁止伪科学当真科学写",
            "禁止技术描写大段无法理解的术语",
            "禁止忽略科技对社会结构的影响",
        ],
        "style_notes": "硬科幻注重逻辑自洽，软科幻注重人文关怀。设定解释要通俗。",
    },
    "历史": {
        "name": "历史",
        "core_elements": ["朝代背景", "政治斗争", "军事战争", "文化礼仪", "真实人物"],
        "pacing": "朝堂/战场/民间三线交织，大事件前铺垫3-5章",
        "key_hooks": ["秘密钩", "承诺钩", "背叛钩"],
        "prohibitions": [
            "禁止用现代口语写古代对话",
            "禁止随意篡改重大历史事件(架空除外)",
            "禁止忽略时代局限性",
        ],
        "style_notes": "语言要有时代感但不能晦涩。注意历史细节的准确性。",
    },
    "末世": {
        "name": "末世",
        "core_elements": ["生存危机", "资源争夺", "人性考验", "变异/丧尸", "建设据点"],
        "pacing": "生存压力持续存在，安全期不超过3章就要新危机",
        "key_hooks": ["危机钩", "背叛钩", "宝物钩"],
        "prohibitions": [
            "禁止末世生活过于轻松",
            "禁止忽略资源消耗(食物/弹药/药品)",
            "禁止所有NPC都是工具人",
        ],
        "style_notes": "灰暗但不绝望。展示人性的复杂面。生存细节要真实。",
    },
    "系统流": {
        "name": "系统流",
        "core_elements": ["系统面板", "任务", "奖励", "升级", "隐藏任务"],
        "pacing": "任务接取 -> 完成过程(有挫折) -> 奖励 -> 新发现。每3-5章一个任务循环",
        "key_hooks": ["升级钩", "宝物钩", "悬念钩"],
        "prohibitions": [
            "禁止系统面板大段粘贴",
            "禁止任务完成毫无难度",
            "禁止系统万能(要有限制和代价)",
        ],
        "style_notes": "系统描述简洁，重点在角色如何利用系统。系统也可以有bug/缺陷。",
    },
    "知乎短篇": {
        "name": "知乎体短篇",
        "core_elements": ["强开头", "反转", "金句", "代入感", "短小精悍"],
        "pacing": "前200字必须抓人，中间快速推进，结尾必须有反转或余味",
        "key_hooks": ["反转钩", "悬念钩", "情感钩"],
        "prohibitions": [
            "禁止慢热开场",
            "禁止冗长的心理描写",
            "禁止结尾解释过多",
        ],
        "style_notes": "开头三句话定生死。节奏极快。每段都有钩子。结尾留白。",
    },
    "游戏": {
        "name": "游戏",
        "core_elements": ["游戏系统", "副本", "公会", "PVP", "隐藏boss"],
        "pacing": "副本攻略为主线，穿插社交和竞技",
        "key_hooks": ["升级钩", "宝物钩", "新角色钩"],
        "prohibitions": [
            "禁止大段贴游戏面板数据",
            "禁止主角无脑碾压",
            "禁止忽略游戏内社交互动",
        ],
        "style_notes": "战斗场面要有策略性。游戏机制融入叙事而非数据堆砌。",
    },
    "无限流": {
        "name": "无限流",
        "core_elements": ["副本世界", "任务规则", "团队协作", "死亡威胁", "积分/道具"],
        "pacing": "每个副本3-8章。准备 -> 进入 -> 探索 -> 危机 -> 破解 -> 结算",
        "key_hooks": ["危机钩", "悬念钩", "反转钩"],
        "prohibitions": [
            "禁止副本规则前后矛盾",
            "禁止死亡没有重量感",
            "禁止队友全是工具人",
        ],
        "style_notes": "规则解谜为核心。死亡要有分量。团队成员各有特色。",
    },
}

# =====================================================================
# AI Word Blacklist (extended)
# =====================================================================

AI_WORD_BLACKLIST: list[str] = [
    # Structural connectors (essay-style)
    "此外", "然而", "值得注意的是", "需要强调的是", "不可忽视",
    "与此同时", "毋庸置疑", "诚然", "显而易见", "不言而喻",
    "总而言之", "综上所述", "总的来说", "不得不说",
    # AI-preferred verbs
    "彰显", "诠释", "赋能", "映射", "折射",
    "承载", "凝聚", "汇聚", "蕴含", "涌现",
    "践行", "赋予", "传递", "构建",
    # AI-preferred emotions
    "不禁", "油然而生", "心潮澎湃", "感慨万千",
    "肃然起敬", "心生敬意", "由衷地",
    "内心深处", "灵魂深处",
    # AI-preferred adjectives
    "璀璨", "瑰丽", "熠熠生辉", "光芒万丈",
    "波澜壮阔", "气势磅礴", "蔚为壮观",
    "深邃", "厚重", "醇厚",
    # AI-preferred descriptions
    "映入眼帘", "嘴角微微上扬", "眼中闪过一丝",
    "脑海中浮现", "空气中弥漫着", "阳光洒在",
    "仿佛在诉说", "似乎在告诉",
    "一股莫名的", "一种说不出的",
    "心中五味杂陈", "百感交集",
    # Filler phrases
    "说实话", "可以说", "毫不夸张地说",
    "值得一提的是", "不得不提",
]

# =====================================================================
# Build Functions
# =====================================================================


def build_writing_prompt(
    active_modules: list[str] | None = None,
    genre: str = "",
    chapter_position: str = "development",
    custom_prohibitions: list[str] | None = None,
    hook_type: str = "",
    include_anti_ai: bool = True,
    word_count_target: int = 3000,
) -> str:
    """Build the complete writing guidance prompt.

    Args:
        active_modules: Which modules to activate. None = all.
        genre: Genre key from GENRE_TEMPLATES.
        chapter_position: One of "opening", "development", "climax", "resolution_hook".
        custom_prohibitions: Additional prohibitions.
        hook_type: Specific hook type to use at chapter end.
        include_anti_ai: Whether to include AI word avoidance guidance.
        word_count_target: Target word count for the chapter.

    Returns:
        Complete writing guidance prompt string.
    """
    parts: list[str] = []

    # Header
    parts.append("=== 写作指导 ===")
    parts.append(f"目标字数: {word_count_target}字")

    # Chapter structure for current position
    if chapter_position in CHAPTER_STRUCTURE:
        parts.append(f"\n【章节结构 - {chapter_position}】")
        parts.append(CHAPTER_STRUCTURE[chapter_position])

    # Active modules
    modules_to_use = active_modules or list(WRITING_MODULES.keys())
    active_rules: list[str] = []

    for module_key in modules_to_use:
        module = WRITING_MODULES.get(module_key)
        if not module:
            continue
        # Include condensed rules (not full examples to save tokens)
        for rule in module["rules"][:3]:
            active_rules.append(rule)

    if active_rules:
        parts.append("\n【核心规则】")
        for i, rule in enumerate(active_rules, 1):
            parts.append(f"{i}. {rule}")

    # Genre template
    if genre and genre in GENRE_TEMPLATES:
        template = GENRE_TEMPLATES[genre]
        parts.append(f"\n【{template['name']}类型要求】")
        parts.append(f"核心元素: {', '.join(template['core_elements'])}")
        parts.append(f"节奏模式: {template['pacing']}")
        if template.get("style_notes"):
            parts.append(f"风格: {template['style_notes']}")
        if template.get("prohibitions"):
            parts.append("类型禁忌:")
            for p in template["prohibitions"]:
                parts.append(f"  - {p}")

    # Prohibitions
    combined_prohibitions = WRITING_PROHIBITIONS[:8]  # Keep top 8 for token savings
    if custom_prohibitions:
        combined_prohibitions.extend(custom_prohibitions)

    parts.append("\n【禁止事项】")
    for p in combined_prohibitions:
        parts.append(f"- {p}")

    # Hook guidance
    if hook_type and hook_type in HOOK_TECHNIQUES:
        technique = HOOK_TECHNIQUES[hook_type]
        parts.append(f"\n【本章结尾钩子 - {hook_type}】")
        parts.append(f"手法: {technique['description']}")
        parts.append(f"示例: {technique['example']}")
    elif chapter_position == "resolution_hook":
        # Suggest hooks for chapter ending
        parts.append("\n【结尾钩子建议】")
        if genre and genre in GENRE_TEMPLATES:
            genre_hooks = GENRE_TEMPLATES[genre].get("key_hooks", [])
            for hk in genre_hooks[:2]:
                if hk in HOOK_TECHNIQUES:
                    parts.append(
                        f"- {hk}: {HOOK_TECHNIQUES[hk]['description']}"
                    )
        else:
            parts.append("- 悬念钩: 抛出未解之谜")
            parts.append("- 危机钩: 新的威胁出现")

    # Anti-AI guidance
    if include_anti_ai:
        parts.append("\n【去AI感要求】")
        parts.append("以下词汇/句式禁止使用(AI痕迹过重):")
        # Show top 15 to save tokens
        blacklist_sample = AI_WORD_BLACKLIST[:15]
        parts.append(f"  {', '.join(blacklist_sample)}...")
        parts.append("替代策略:")
        parts.append("  - 用具体动作/细节替代抽象形容")
        parts.append("  - 用角色特色口语替代书面语")
        parts.append("  - 变换句式开头，不要连续用'他/她'开头")
        parts.append("  - 减少'的'字使用密度")

    return "\n".join(parts)


def get_genre_template(genre: str) -> dict[str, Any] | None:
    """Get the template for a specific genre."""
    return GENRE_TEMPLATES.get(genre)


def get_available_genres() -> list[str]:
    """Get list of all available genre keys."""
    return list(GENRE_TEMPLATES.keys())


def get_hook_techniques() -> dict[str, dict[str, str]]:
    """Get all available hook techniques."""
    return HOOK_TECHNIQUES


def get_module_details(module_key: str) -> dict[str, Any] | None:
    """Get details for a specific writing module."""
    return WRITING_MODULES.get(module_key)


def build_chapter_brief(
    genre: str,
    chapter_idx: int,
    total_chapters: int,
    chapter_outline: dict,
) -> str:
    """Build a brief chapter instruction based on position in the overall arc.

    Args:
        genre: The genre of the novel.
        chapter_idx: Current chapter index.
        total_chapters: Total number of chapters in the volume.
        chapter_outline: The outline for this chapter.

    Returns:
        A brief instruction string for the chapter position.
    """
    # Determine chapter position in the arc
    if total_chapters <= 0:
        position = "development"
    else:
        progress = chapter_idx / total_chapters
        if progress < 0.05:
            position = "opening"
        elif progress > 0.85:
            position = "resolution_hook"
        elif 0.6 < progress <= 0.85:
            position = "climax"
        else:
            position = "development"

    # Determine appropriate hook type based on genre and position
    hook_type = ""
    if genre in GENRE_TEMPLATES:
        hooks = GENRE_TEMPLATES[genre].get("key_hooks", [])
        if hooks:
            # Rotate hooks based on chapter index
            hook_type = hooks[chapter_idx % len(hooks)]

    return build_writing_prompt(
        genre=genre,
        chapter_position=position,
        hook_type=hook_type,
        word_count_target=chapter_outline.get("target_words", 3000),
    )
