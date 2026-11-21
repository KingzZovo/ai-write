from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from collections import Counter
from typing import Any

from app.services.prose_quality_rules import regex_patterns_for

SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
QUOTE_RE = re.compile(r"[“\"]([^”\"]+)[”\"]")
SHORT_SENT_LIMIT = 12
SHORT_RUN_TARGET = 4
SHORT_DIALOGUE_LIMIT = 18
DIALOGUE_SYMMETRY_RUN_LIMIT = 8
PERFECT_COMEBACK_RUN_LIMIT = 6
PERFECT_COMEBACK_CUE_LIMIT = 2
ACTION_QUOTE_PARAGRAPH_RATE_LIMIT = 0.45
ACTION_QUOTE_PARAGRAPH_MIN_COUNT = 10
TIGHT_QA_PAIR_LIMIT = 12
TIGHT_QA_GAP_LIMIT = 120
SHORT_DIALOGUE_DENSITY_LIMIT = 0.52
SHORT_DIALOGUE_DENSITY_MIN_QUOTES = 20
SHORT_PARAGRAPH_DENSITY_LIMIT = 0.40
SHORT_PARAGRAPH_DENSITY_MIN_PARAGRAPHS = 20
SHORT_PARAGRAPH_LIMIT = 30

VERB_WATCHLIST = [
    "一翻", "钻过", "掠出", "探手", "反手", "肘下", "腋下", "腕", "膝", "踝",
    "贴着", "半寸", "半息", "半指", "寸许", "尺许", "错身", "旋身",
]
SPACE_WATCHLIST = ["半寸", "半息", "半指", "寸许", "尺许", "肘下", "腋下"]
FORBIDDEN_COLLOCATIONS = [
    "供纸案匣", "外目三", "手腕一翻", "从肘下钻过",
]
EXPOSITION_TERMS = ["制度", "规制", "衙门", "名册", "卷宗", "供奉", "守备", "门人", "账书", "香火"]
ACTION_DIALOGUE_BEAT_VERBS = [
    "弯腰", "抬手", "抬眼", "低头", "抬头", "转头", "看了", "扫了",
    "拿起", "放下", "推开", "按住", "拨了", "绕了", "擦了", "皱眉",
    "摇头", "笑了", "瞥了", "停住",
]
CHEAP_WIT_PATTERNS = [
    r"踩(?:烂|坏|碎|脏|扁|塌|坏了|烂了).{0,6}算你买",
    r"算你买",
    r"算谁卖",
    r"You break it, you buy it",
    r"损坏照买",
]
PERFECT_COMEBACK_CUES = [
    r"看一行",
    r"多看",
    r"认错",
    r"认对",
    r"算你买",
    r"算谁卖",
    r"别装",
    r"装什么",
    r"合上",
    r"赖账",
]
PROP_FIDDLING_PATTERNS = [
    r"拨.{0,6}算盘|算盘.{0,6}拨",
    r"绕.{0,6}绳|绳.{0,6}绕",
    r"擦.{0,6}砚|砚.{0,6}擦|挪.{0,6}砚|砚.{0,6}挪",
    r"摸.{0,6}(杯|账|纸|桌|门|书)",
    r"把.{0,6}(算盘|绳|砚).{0,8}(放|拨|推|挪|按|扣)",
]
EXPLICIT_PAUSE_MARKERS = [
    "安静了一", "安静片刻", "沉默", "没有立刻", "一小会儿", "半晌",
    "停了一下", "顿了顿",
]
DIRECT_INTENT_EXPOSITION_PATTERNS = [
    r"你怕我", r"你想让", r"你是想", r"我想让你", r"你不就是",
    r"你真正", r"你其实", r"你故意",
]
MOTIVE_EXPOSITION_PATTERNS = [
    r"你就是想",
    r"你只是想",
    r"你无非是想",
    r"你根本就是想",
    r"你刚才.{0,20}现在.{0,20}你就是想",
    r"你刚才.{0,20}现在.{0,20}就是想",
    r"赖账",
    r"耍赖",
    r"翻旧账",
    r"底层逻辑",
    r"本质上",
    r"无非是",
]
STATIC_SPATIAL_MAPPING_PATTERNS = [
    r"[一二三四五六七八九十两几0-9]+步(?:外|之外|开外|远|之遥)?",
    r"[一二三四五六七八九十两几0-9]+尺(?:外|之外|开外|远|之遥|宽|长|厚|距离)?",
    r"[一二三四五六七八九十两几0-9]+丈(?:外|之外|开外|远|之遥)?",
    r"[一二三四五六七八九十两几0-9]+指(?:宽|长|厚|距离)?",
    r"半步(?:外|之外|开外|远|之遥)",
    r"(?:脚尖|鞋尖|鞋底|身子|人|他|她).{0,14}(?:影子外|门槛外|线外|柜台外)",
    r"(?:影子外|柜台影子外)",
    r"(?:几寸|[一二三四五六七八九十两几0-9]+寸(?:外|远|宽|长|开外|距离)?)",
]
BIOGRAPHICAL_INFODUMP_PATTERNS = [
    r"(?:从)?[一二三四五六七八九十百0-9]+岁到[一二三四五六七八九十百0-9]+岁",
    r"从.{0,8}岁.{0,8}到.{0,8}岁",
    r"[“\"][^”\"]{70,}(?:我替|我从|我在|祖母|家里人|小时候|十年|跑腿)[^”\"]*[”\"]",
]
ACTION_QUOTE_PARAGRAPH_PATTERNS = [
    r"把.{0,18}(?:往|向|一|进|到|在|过|开|回|上|下)",
    r"(?:抱着|攥紧|咬得|探|递|横|压|撑|托|拢|缩|靠|停在|落在|站在|让开|让出|抬|伸|转|盯|扫|挪|推|收|翻|挑|点|按|送|拿|放)",
    r"(?:水珠|雨水|油灯|灯|纸边|伞柄|竹尺|竹签).{0,16}(?:滴|落|晃|横|压|停|亮)",
]
SPEECH_ATTRIBUTION_ONLY_RE = re.compile(
    r"^\s*[\u4e00-\u9fff]{1,8}(?:道|说|问|答|回)(?:得.{0,4})?[，。,:：]?\s*$"
)
PROCEDURAL_EXPOSITION_TERMS = [
    "北仓旧库",
    "灰河码头",
    "三号箱",
    "市书会",
    "账房",
    "灯籍",
    "回封",
    "封包",
    "封条",
    "封存",
    "待验",
    "候验",
    "错箱",
    "规矩",
    "管事",
    "转手点",
    "收摊",
    "包货",
    "货",
    "纸坊",
    "纸口",
    "纸路",
    "旧孔",
    "新线",
    "补线",
    "线口",
    "见证",
    "单拿",
]
PROCEDURAL_FLOW_MARKERS = ["若", "先", "再", "就", "不许", "不得", "只能", "必须", "一并"]
ACTION_DIALOGUE_BEAT_LIMIT = 14
STORY_BIBLE_TERMS = [
    "执行者",
    "血裔",
    "旧神",
    "奥丁",
    "神裔",
    "血脉",
    "校准",
    "未知X",
    "未知 X",
    "瓦尔基里",
    "托尔",
    "学院",
    "暗面学院",
    "继承人",
]
STORY_BIBLE_PUBLIC_CONTEXT_TERMS = [
    "广告",
    "海报",
    "新闻",
    "广播",
    "电视",
    "字幕",
    "网上",
    "帖子",
    "热搜",
    "论坛",
    "路人",
    "闲聊",
    "听说",
    "传开",
    "收银台",
    "便利店",
    "小区",
    "凉亭",
    "楼下",
    "旁边人",
    "街上",
    "路口",
    "店里",
]
_DIRECTIONAL_LISTING_GROUPS = {
    "left": [r"左(?:边|手|侧|墙|面|头)"],
    "right": [r"右(?:边|手|侧|墙|面|头)"],
    "east": [r"东(?:头|边|面|侧)"],
    "west": [r"西(?:头|边|面|侧)"],
    "front": [r"前(?:头|面|方|边)"],
    "back": [r"后(?:头|面|方|边)"],
    "side": [r"旁边", r"边上", r"侧边"],
}
_DIRECTIONAL_LISTING_OPPOSITE_PAIRS = [
    ("left", "right"),
    ("east", "west"),
    ("front", "back"),
]
_DIRECTIONAL_LISTING_ENV_CUES = [
    "信箱",
    "墙",
    "通知",
    "连廊",
    "楼梯",
    "门",
    "楼道",
    "走廊",
    "巷",
    "路",
    "桥",
    "柜台",
    "货架",
    "窗",
    "院",
    "小区",
    "铺子",
    "门牌",
]
AWKWARD_REGISTER_PATTERNS = [
    r"别挡锅",
    r"别碰那边",
    r"别进楼",
    r"把手机按黑",
    r"按黑",
    r"带了急",
    r"收租截图",
]
LIMITED_POV_LEAK_PATTERNS = [
    r"才想起他",
    r"才想起.{0,16}他还在队伍里",
    r"没人记得",
    r"没人问他",
    r"忽然觉得自己和那道水印差不多",
    r"和那道水印差不多",
]
HARDSHIP_STACK_TERMS = [
    "老师",
    "目光滑走",
    "门卫",
    "敷衍",
    "班群",
    "无声",
    "食堂",
    "食堂阿姨",
    "漏人",
    "房东",
    "催租",
    "没人问",
    "没人记得",
    "点名册",
    "值日表",
]
RESOURCE_CONTINUITY_PATTERNS = [
    r"(?:叫车|打车|网约车|出租车).{0,80}(?:口袋里|兜里).{0,30}(?:只剩|零钱|硬币|几枚)",
    r"(?:口袋里|兜里).{0,30}(?:只剩|零钱|硬币|几枚).{0,80}(?:叫车|打车|网约车|出租车)",
    r"(?:手机上|页面|软件|平台).{0,20}(?:叫车|打车).{0,80}(?:只有|只剩).{0,20}(?:零钱|硬币)",
]
SCENE_PLAUSIBILITY_PATTERNS = [
    r"(?s)(?:没叫到车|附近没车|暂无车辆).{0,120}(?:钻进|坐上|拦下).{0,16}出租车",
    r"(?s)(?:钻进|坐上|拦下).{0,16}出租车.{0,120}(?:没叫到车|附近没车|暂无车辆)",
]
ACTION_CAUSALITY_PATTERNS = [
    r"别挡着锅.{0,20}雨.{0,8}(?:溅|飘|甩).{0,8}(?:进|到)",
    r"挡着锅.{0,20}雨.{0,8}(?:溅|飘|甩).{0,8}(?:进|到)",
    r"挡着锅.{0,20}水全甩.{0,8}(?:进|到)",
]
MUNDANE_REGISTER_PATTERNS = [
    r"偏门已经关",
    r"光不强",
    r"光不亮",
    r"那点光.{0,8}却稳",
    r"试错",
    r"声音被雨拉得很散",
    r"胃里却更空",
]
MOTIVATION_GAP_PATTERNS = [
    r"(?:看见|看到).{0,16}(?:门|门缝|那点光|黄光).{0,40}(?:走过去推门|推门|跨进去)",
    r"(?:光不强|光不亮|那点光.{0,8}却稳|那道光.{0,8}却稳).{0,50}(?:走过去|推门|跨进去|推门进去)",
    r"门缝里(?:没|没有)潮气.{0,40}(?:走过去|推门|跨进去|推门进去)",
]
TRANSPORT_LOGIC_PATTERNS = [
    r"(?:末班车|末班公交|末班地铁)(?:已经)?(?:早)?(?:走了|开走了|没了|停了|错过了).{0,40}(?:换乘|转车).{0,20}(?:赶不上|来不及|错过)",
    r"(?:换乘|转车).{0,20}(?:赶不上|来不及|错过).{0,40}(?:末班车|末班公交|末班地铁)(?:已经)?(?:早)?(?:走了|开走了|没了|停了|错过了)",
]
SEMANTIC_COLLOCATION_PATTERNS = [
    r"透不出灯(?!光)",
    r"(?:价格|车费|房费|房价|账单|金额|价钱).{0,14}(?:更)?难看",
    r"页面刷出来.{0,20}(?:难看|脸色)",
    r"背包.{0,12}放干|放干.{0,12}背包",
    r"(?:可以|能|任何).{0,12}借口.{0,12}(?:声音|留下)",
    r"手机.{0,8}还活着",
]
SHELTER_COST_LOGIC_PATTERNS = [
    r"(?s)(?:旅馆|宾馆|酒店|前台|住一晚|真进去).{0,160}(?:背包.{0,12}放干|没力气.{0,24}脸色|碰这种脸色|从头看到脚)",
    r"(?s)(?:背包.{0,12}放干|没力气.{0,24}脸色|碰这种脸色).{0,120}(?:旅馆|宾馆|酒店|前台|押金|证件)",
]
ABSTRACT_EVASION_PATTERNS = [
    r"(?:慢慢)?试错",
    r"借口留下来",
    r"(?:可以|能|任何).{0,12}借口.{0,12}声音",
    r"(?:碰|看).{0,8}这种脸色|这种脸色",
    r"没力气再去碰",
    r"(?:价格|车费|房费|金额).{0,12}难看",
]
# Sourced from the cross-project prose quality rule catalog (single source of
# truth) so this checker stays in sync with the generation prompts and gate.
PSEUDO_LITERARY_REGISTER_PATTERNS = list(regex_patterns_for("plain_contemporary_chinese"))

# Reader-feedback (2026-07): the anti-AI rules overcorrected into a new machine
# flavor — micro-action overload (推眼镜×3 / 摸茧 / 膝盖轻响×2 in one chapter),
# repeated rare phrases, and translation-skeleton phrasing (像是介于两者之间).
# 微动作预算: budget for "small action beats" per 1000 chars. WARN-level: the
# density metric feeds the rewrite penalty and preflight prompt, not `passed`.
MICRO_ACTION_BEAT_PATTERNS = [
    r"推了推眼镜|推了下眼镜|扶了扶眼镜|眼镜往上推|把眼镜.{0,4}推",
    r"摸了摸",
    r"捏了捏|捏紧",
    r"蹭了蹭",
    r"搓了搓",
    r"挠了挠|挠头",
    r"(?:关节|指节|膝盖|脖颈)[^。！？\n]{0,4}(?:轻响|作响|一响|响了|咔)",
    r"(?:指腹|指尖)[^。！？\n]{0,6}(?:贴|探|蹭|抠|摩挲|擦)",
    r"摩挲",
    r"抿了抿",
    r"敲了敲",
    r"扯了扯|拽了拽",
    r"攥了攥|攥紧",
    r"捻了捻",
    r"掐了掐",
    r"(?:摸|蹭|抠)[^。！？\n]{0,4}茧",
]
MICRO_ACTION_DENSITY_LIMIT = 3.0  # beats per 1000 chars
MICRO_ACTION_MIN_HITS = 3  # short-snippet guard: one beat alone never flags

# repeated_rare_phrase: a distinctive 4+ char CJK n-gram recurring 3+ times in
# one chapter (膝盖轻响×3, 焊锡茧×3) reads as a verbal tic. Function-char guard
# keeps ordinary high-frequency constructions (的/了/他/说…) out of the count.
REPEATED_RARE_PHRASE_MIN_COUNT = 3
_RARE_PHRASE_NGRAM_SIZES = (4, 5, 6)
_RARE_PHRASE_GUARD_CHARS = set(
    "的一了是在不有人我他她它你们这那就都而及与着或个上下来去到得地过"
    "说道么什呢吧啊吗把被让给从对向和跟为于之其还又也很没要会能可以"
    "看走站坐盯笑摇点抬低转回开关拿放伸手眼头脸声里外前后边时候自己"
)
_CJK_RUN_RE = re.compile(r"[一-鿿]{4,}")

# translationese: English-skeleton calques. Violation-level (blocks like meta
# leakage): they never belong in natural Chinese narration.
TRANSLATIONESE_MARKER_PATTERNS = [
    r"介于.{0,12}之间",
    r"某种意义上",
    r"从某种程度",
    r"在.{0,8}的意义上",
    r"事实上，",
]

# --- Defect-class immunity (2026-07, docs/DEFECT_GOVERNANCE.md) ---
# Class: era/register bleed. A bound style-reference book from another era can
# bleed period vocabulary into a modern/near-future novel (live case: 「约莫
# 两炷香工夫」 in a near-future chapter), or modern units into a 古风 novel.
# Deterministic lexicon check gated on the project genre: active only when the
# genre string clearly classifies as modern or period; unknown/ambiguous genre
# keeps the detector OFF (never guess). WARN-level like micro_action: feeds
# _quality_penalty + rewrite/preflight instructions, never blocks on its own.
_MODERN_GENRE_KEYWORDS = (
    "现代", "都市", "近未来", "未来", "科幻", "赛博", "星际", "末世", "末日",
    "校园", "职场", "urban", "scifi", "sci-fi", "sci_fi", "cyberpunk", "modern",
    "future",
)
_PERIOD_GENRE_KEYWORDS = (
    "古风", "古代", "古言", "仙侠", "修仙", "修真", "武侠", "历史", "宫廷",
    "xianxia", "wuxia", "historical", "history", "ancient",
)
# 古风计时/度量 terms flagged in a modern/near-future/scifi/urban setting.
# Conservative: era-neutral survivals (一刻钟, 三更半夜) are deliberately absent.
MODERN_SETTING_FLAGS = [
    r"[一二三四五六七八九半两几0-9]*炷香",   # 一炷香/两炷香（工夫）
    r"[一半两]盏茶",
    r"半刻钟",
    r"[一二三四五]更天",
    r"时辰",
    # 地支+时 (戌时/亥时…). Lookarounds keep 下午时分/中午时/晌午时/星辰时代 safe.
    r"(?<![上下中傍晌星生凌])[子丑寅卯辰巳午未申酉戌亥]时(?![分候间钟段代])",
    r"[一二三四五六七八九十百千两几0-9]+(?:丈|里地)",
    r"丈余|丈许",
]
# Modern terms flagged in a 古风/仙侠/历史/武侠 setting.
PERIOD_SETTING_FLAGS = [
    r"分钟",
    r"小时(?!候)",   # 小时候 is era-neutral
    r"公里",
    r"千米",
    r"百分之[一二三四五六七八九十百0-9]+|百分比",
    r"手机",
    r"电话",
]
ERA_REGISTER_TOP_TERMS = 8


def classify_genre_register(genre_hint: str | None) -> str:
    """Map a project genre string / genre_profile_code to a register class.

    Returns ``"modern"``, ``"period"``, or ``""`` when the genre is unknown or
    matches both keyword families (ambiguous) — in which case the era detector
    stays inactive rather than guessing.
    """
    hint = (genre_hint or "").strip().lower()
    if not hint:
        return ""
    is_modern = any(keyword in hint for keyword in _MODERN_GENRE_KEYWORDS)
    is_period = any(keyword in hint for keyword in _PERIOD_GENRE_KEYWORDS)
    if is_modern == is_period:
        return ""
    return "modern" if is_modern else "period"


def _era_register_conflicts(text: str, register_class: str) -> Counter[str]:
    if register_class == "modern":
        patterns = MODERN_SETTING_FLAGS
    elif register_class == "period":
        patterns = PERIOD_SETTING_FLAGS
    else:
        return Counter()
    counts: Counter[str] = Counter()
    for pattern in patterns:
        for match in re.finditer(pattern, text or ""):
            counts[match.group(0)] += 1
    return counts


# Class: scene-seam duplication. Adjacent scenes re-narrate the same beat (live
# case: two consecutive paragraphs both restating 声音断了+电流声). Deterministic
# near-duplicate narration-sentence detection: quoted speech is masked out first
# (so a character deliberately echoing a line is never flagged), then sentences
# whose compact narration is >= SEAM_DUP_MIN_SENT_CHARS are compared pairwise
# within a ~SEAM_DUP_WINDOW_CHARS window on char-bigram Jaccard similarity.
# WARN-level: penalty + rewrite instruction (合并或删除复述句), never blocks alone.
SEAM_DUP_MIN_SENT_CHARS = 10
SEAM_DUP_SIMILARITY = 0.6
SEAM_DUP_WINDOW_CHARS = 800
SEAM_DUP_TOP_PAIRS = 3
_SEAM_SENT_END_RE = re.compile(r"[。！？!?；;\n]+")
_SEAM_COMPACT_RE = re.compile(r"[\s□，、,．.：:…—-]+")


def _char_bigrams(compact: str) -> set[str]:
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def _seam_duplication(text: str) -> tuple[int, list[str]]:
    """Return (near_duplicate_pair_count, top_pairs_trimmed)."""
    src = text or ""
    if not src.strip():
        return 0, []
    # Mask quoted dialogue with a filler of equal length so offsets survive and
    # quote content never participates in narration similarity.
    masked = QUOTE_RE.sub(lambda m: "□" * len(m.group(0)), src)
    entries: list[tuple[int, str, set[str]]] = []
    cursor = 0
    boundaries = [m.span() for m in _SEAM_SENT_END_RE.finditer(masked)]
    boundaries.append((len(masked), len(masked)))
    for b_start, b_end in boundaries:
        segment = masked[cursor:b_start]
        seg_start = cursor
        cursor = b_end
        compact = _SEAM_COMPACT_RE.sub("", segment)
        if len(compact) < SEAM_DUP_MIN_SENT_CHARS:
            continue
        entries.append((seg_start, src[seg_start:b_start].strip(), _char_bigrams(compact)))
    pair_count = 0
    scored: list[tuple[float, str]] = []
    for i, (start_i, raw_i, grams_i) in enumerate(entries):
        for j in range(i + 1, len(entries)):
            start_j, raw_j, grams_j = entries[j]
            if start_j - start_i > SEAM_DUP_WINDOW_CHARS:
                break
            union = grams_i | grams_j
            if not union:
                continue
            similarity = len(grams_i & grams_j) / len(union)
            if similarity >= SEAM_DUP_SIMILARITY:
                pair_count += 1
                scored.append((similarity, f"{raw_i[:24]}…↔{raw_j[:24]}…"))
    scored.sort(key=lambda kv: -kv[0])
    return pair_count, [pair for _, pair in scored[:SEAM_DUP_TOP_PAIRS]]


# Class: mundane plausibility / 感知锚定碎片。无主语、以身体部位或器物开头的
# 分镜短句（活体：「鞋底磨薄了，脚跟先落地。」）在重写时丢掉感知动词后会漂成
# 镜头注释。只打模式②：句长 ≤14 字、无主语代词、以身体/器物名词开头、不含感知
# 动词，且独立成段或紧跟对白后句首。WARN 级；误报比漏报更糟，词表保持保守。
FLOATING_FRAGMENT_MAX_CHARS = 14
_FLOATING_START_PREFIXES: tuple[str, ...] = (
    "鞋底", "鞋尖", "鞋跟", "脚跟", "脚尖", "脚掌", "脚背", "脚踝", "靴底", "靴跟",
    "手指", "指尖", "指节", "指腹", "手背", "手心", "掌心", "虎口", "手肘", "肘弯",
    "小臂", "大臂", "腕骨", "腕口", "眼皮", "眼角", "眉心", "眉毛", "睫毛", "瞳孔",
    "鼻尖", "鼻翼", "嘴角", "唇角", "下巴", "额角", "太阳穴", "耳廓", "耳垂", "后颈",
    "肩头", "肩背", "后背", "脊背", "锁骨", "肋骨", "腰侧", "后腰", "膝头", "膝盖",
    "小腿", "大腿", "裤脚", "衣角", "衣摆", "袖口", "袖管", "帽檐", "发梢", "喉结",
)
_FLOATING_SUBJECT_START_RE = re.compile(
    r"^(他|她|我|你|它|咱|俺|您|谁|他们|她们|我们|你们|它们|他俩|她俩|两人|三人|众人|这人|那人)"
)
_FLOATING_PERCEPTION_RE = re.compile(
    r"听出|听见|听到|听得|看见|看到|望见|认出|认得|察觉|感到|感觉到|注意到|留意到|"
    r"闻见|闻到|瞧见|瞅见|瞥见|发现"
)
_FLOATING_COMPACT_RE = re.compile(r"[\s，。！？、；：,.!?;:“”\"'…—\-·]+")
_FLOATING_CLAUSE_SPLIT_RE = re.compile(r"[，、；,;]")


def _is_dialogue_paragraph(paragraph: str) -> bool:
    s = paragraph.strip()
    return bool(s) and (("“" in s) or ('"' in s) or ("”" in s))


def _clause_starts_with_body_or_prop(clause: str) -> bool:
    c = clause.strip()
    return bool(c) and c.startswith(_FLOATING_START_PREFIXES)


def _looks_like_floating_fragment(sentence: str) -> bool:
    """两段无主语分镜短句：身体/器物起句 + 再切一刀，才像镜头注释。

    单句「门缝里透出一线光」「手指敲了两下」是正常散文，不得命中。
    """
    s = sentence.strip()
    if not s or s.startswith(("“", '"')):
        return False
    compact = _FLOATING_COMPACT_RE.sub("", s)
    if not (1 <= len(compact) <= FLOATING_FRAGMENT_MAX_CHARS):
        return False
    if _FLOATING_SUBJECT_START_RE.match(s):
        return False
    if _FLOATING_PERCEPTION_RE.search(s):
        return False
    clauses = [part.strip() for part in _FLOATING_CLAUSE_SPLIT_RE.split(s) if part.strip()]
    if len(clauses) < 2:
        return False
    return all(_clause_starts_with_body_or_prop(clause) for clause in clauses[:2])


def _floating_fragments(text: str) -> tuple[int, list[str]]:
    """Return (count, up to 3 sample fragments). Conservative: miss > false positive."""
    paragraphs = _paragraphs(text)
    hits: list[str] = []
    for idx, para in enumerate(paragraphs):
        sents = [part.strip() for part in SENT_SPLIT_RE.split(para) if part.strip()]
        if not sents:
            continue
        after_dialogue = idx > 0 and _is_dialogue_paragraph(paragraphs[idx - 1])
        standalone = len(sents) == 1
        for sent_i, sent in enumerate(sents):
            if not _looks_like_floating_fragment(sent):
                continue
            if standalone or (after_dialogue and sent_i == 0):
                hits.append(sent)
    return len(hits), hits[:3]


@dataclass
class ChineseProseMechanicsReport:
    passed: bool = False
    forbidden_collocation_count: int = 0
    forbidden_collocation_counts: dict[str, int] = field(default_factory=dict)
    space_watchlist_hits: int = 0
    sentence_count: int = 0
    short_sentence_run_max: int = 0
    short_sentence_runs_over_target: int = 0
    verb_watchlist_hits: int = 0
    exposition_cluster_risk: int = 0
    long_quote_segments_gt80: int = 0
    duplicate_quote_segments_gt20: int = 0
    action_dialogue_beat_count: int = 0
    prop_fiddling_count: int = 0
    explicit_pause_marker_count: int = 0
    direct_intent_exposition_count: int = 0
    motive_exposition_count: int = 0
    cheap_wit_count: int = 0
    dialogue_symmetry_risk_count: int = 0
    perfect_comeback_run_count: int = 0
    duplicate_short_dialogue_ladder_count: int = 0
    spatial_mapping_count: int = 0
    biographical_infodump_count: int = 0
    paragraph_count: int = 0
    quote_count: int = 0
    action_quote_paragraph_count: int = 0
    action_quote_paragraph_rate: float = 0.0
    tight_qa_pair_count: int = 0
    short_dialogue_density: float = 0.0
    short_paragraph_density: float = 0.0
    interiority_monologue_rate: float = 0.0
    repeated_realization_run: int = 0
    dialogue_paragraph_rate: float = 0.0
    procedural_exposition_cluster_count: int = 0
    story_bible_leakage_count: int = 0
    meta_structure_leakage_count: int = 0
    directional_listing_count: int = 0
    awkward_register_count: int = 0
    limited_pov_leak_count: int = 0
    mundane_logic_violation_count: int = 0
    hardship_stack_count: int = 0
    resource_continuity_count: int = 0
    mundane_register_count: int = 0
    action_causality_count: int = 0
    motivation_gap_count: int = 0
    scene_plausibility_count: int = 0
    transport_logic_count: int = 0
    semantic_collocation_count: int = 0
    shelter_cost_logic_count: int = 0
    abstract_evasion_count: int = 0
    pseudo_literary_register_count: int = 0
    plain_contemporary_violation_count: int = 0
    duplicate_explanation_span_count: int = 0
    micro_action_beat_count: int = 0
    micro_action_density_per_1000: float = 0.0
    micro_action_overload: bool = False
    repeated_rare_phrase_count: int = 0
    repeated_rare_phrases: dict[str, int] = field(default_factory=dict)
    translationese_marker_count: int = 0
    era_register_class: str = ""
    era_register_conflict_count: int = 0
    era_register_conflicts: dict[str, int] = field(default_factory=dict)
    seam_duplication_count: int = 0
    seam_duplication_pairs: list[str] = field(default_factory=list)
    floating_fragment_count: int = 0
    floating_fragment_samples: list[str] = field(default_factory=list)
    paragraph_risks: list[str] = field(default_factory=list)

    @property
    def pass_(self) -> bool:
        return self.passed

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "assessment_scope": "output_structure_only",
            "literary_acceptance": "not_assessed",
            "pass": self.passed,
            "passed": self.passed,
            "forbidden_collocation_count": self.forbidden_collocation_count,
            "forbidden_collocation_counts": dict(self.forbidden_collocation_counts),
            "space_watchlist_hits": self.space_watchlist_hits,
            "sentence_count": self.sentence_count,
            "short_sentence_run_max": self.short_sentence_run_max,
            "short_sentence_runs_over_target": self.short_sentence_runs_over_target,
            "verb_watchlist_hits": self.verb_watchlist_hits,
            "exposition_cluster_risk": self.exposition_cluster_risk,
            "long_quote_segments_gt80": self.long_quote_segments_gt80,
            "duplicate_quote_segments_gt20": self.duplicate_quote_segments_gt20,
            "action_dialogue_beat_count": self.action_dialogue_beat_count,
            "prop_fiddling_count": self.prop_fiddling_count,
            "explicit_pause_marker_count": self.explicit_pause_marker_count,
            "direct_intent_exposition_count": self.direct_intent_exposition_count,
            "motive_exposition_count": self.motive_exposition_count,
            "cheap_wit_count": self.cheap_wit_count,
            "dialogue_symmetry_risk_count": self.dialogue_symmetry_risk_count,
            "perfect_comeback_run_count": self.perfect_comeback_run_count,
            "duplicate_short_dialogue_ladder_count": self.duplicate_short_dialogue_ladder_count,
            "spatial_mapping_count": self.spatial_mapping_count,
            "biographical_infodump_count": self.biographical_infodump_count,
            "paragraph_count": self.paragraph_count,
            "quote_count": self.quote_count,
            "action_quote_paragraph_count": self.action_quote_paragraph_count,
            "action_quote_paragraph_rate": self.action_quote_paragraph_rate,
            "tight_qa_pair_count": self.tight_qa_pair_count,
            "short_dialogue_density": self.short_dialogue_density,
            "short_paragraph_density": self.short_paragraph_density,
            "interiority_monologue_rate": self.interiority_monologue_rate,
            "repeated_realization_run": self.repeated_realization_run,
            "dialogue_paragraph_rate": self.dialogue_paragraph_rate,
            "procedural_exposition_cluster_count": self.procedural_exposition_cluster_count,
            "story_bible_leakage_count": self.story_bible_leakage_count,
            "meta_structure_leakage_count": self.meta_structure_leakage_count,
            "directional_listing_count": self.directional_listing_count,
            "awkward_register_count": self.awkward_register_count,
            "limited_pov_leak_count": self.limited_pov_leak_count,
            "mundane_logic_violation_count": self.mundane_logic_violation_count,
            "hardship_stack_count": self.hardship_stack_count,
            "resource_continuity_count": self.resource_continuity_count,
            "mundane_register_count": self.mundane_register_count,
            "action_causality_count": self.action_causality_count,
            "motivation_gap_count": self.motivation_gap_count,
            "scene_plausibility_count": self.scene_plausibility_count,
            "transport_logic_count": self.transport_logic_count,
            "semantic_collocation_count": self.semantic_collocation_count,
            "shelter_cost_logic_count": self.shelter_cost_logic_count,
            "abstract_evasion_count": self.abstract_evasion_count,
            "pseudo_literary_register_count": self.pseudo_literary_register_count,
            "plain_contemporary_violation_count": self.plain_contemporary_violation_count,
            "duplicate_explanation_span_count": self.duplicate_explanation_span_count,
            "micro_action_beat_count": self.micro_action_beat_count,
            "micro_action_density_per_1000": self.micro_action_density_per_1000,
            "micro_action_overload": self.micro_action_overload,
            "repeated_rare_phrase_count": self.repeated_rare_phrase_count,
            "repeated_rare_phrases": dict(self.repeated_rare_phrases),
            "translationese_marker_count": self.translationese_marker_count,
            "era_register_class": self.era_register_class,
            "era_register_conflict_count": self.era_register_conflict_count,
            "era_register_conflicts": dict(self.era_register_conflicts),
            "seam_duplication_count": self.seam_duplication_count,
            "seam_duplication_pairs": list(self.seam_duplication_pairs),
            "floating_fragment_count": self.floating_fragment_count,
            "floating_fragment_samples": list(self.floating_fragment_samples),
            "paragraph_risks": list(self.paragraph_risks),
        }


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT_RE.split(text or "") if s.strip()]


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n+", text or "") if p.strip()]


def _is_pure_dialogue_sentence(sentence: str) -> bool:
    s = sentence.strip()
    return s.startswith(("“", '"')) or bool(re.fullmatch(r"[“\"].+[”\"]", s))


def _short_run_metrics(sents: list[str]) -> tuple[int, int]:
    max_run = 0
    over = 0
    run = 0
    for s in sents:
        if _is_pure_dialogue_sentence(s):
            if run > SHORT_RUN_TARGET:
                over += 1
            run = 0
            continue
        is_short = len(re.sub(r"\s+", "", s)) <= SHORT_SENT_LIMIT
        if is_short:
            run += 1
            max_run = max(max_run, run)
        else:
            if run > SHORT_RUN_TARGET:
                over += 1
            run = 0
    if run > SHORT_RUN_TARGET:
        over += 1
    return max_run, over


def _count_regex_hits(text: str, patterns: list[str]) -> int:
    return sum(len(re.findall(pattern, text)) for pattern in patterns)


def _count_unique_regex_hits(text: str, patterns: list[str]) -> int:
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        spans.extend(match.span() for match in re.finditer(pattern, text))
    if not spans:
        return 0
    spans.sort()
    merged: list[tuple[int, int]] = []
    current_start, current_end = spans[0]
    for start, end in spans[1:]:
        if start < current_end:
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return len(merged)


def _quote_contents(text: str) -> list[str]:
    return [match.group(1) for match in QUOTE_RE.finditer(text or "")]


def _round_rate(value: float) -> float:
    return round(value, 3)


def _count_action_dialogue_beats(text: str) -> int:
    verbs = "|".join(re.escape(verb) for verb in ACTION_DIALOGUE_BEAT_VERBS)
    return len(re.findall(rf"(?:{verbs}).{{0,24}}[，。]?[“\"]", text))


def _has_action_quote_binding(paragraph: str) -> bool:
    if not re.search(r"[“\"]", paragraph or ""):
        return False
    narration = QUOTE_RE.sub("", paragraph or "").strip()
    if not narration or SPEECH_ATTRIBUTION_ONLY_RE.fullmatch(narration):
        return False
    return any(re.search(pattern, narration) for pattern in ACTION_QUOTE_PARAGRAPH_PATTERNS)


def _action_quote_paragraph_metrics(paragraphs: list[str]) -> tuple[int, float]:
    count = sum(1 for p in paragraphs if _has_action_quote_binding(p))
    rate = count / max(len(paragraphs), 1)
    return count, _round_rate(rate)


def _count_tight_qa_pairs(text: str) -> int:
    matches = list(QUOTE_RE.finditer(text or ""))
    count = 0
    for idx, match in enumerate(matches[:-1]):
        current = match.group(1).strip()
        if "？" not in current and "?" not in current:
            continue
        next_match = matches[idx + 1]
        gap = (text or "")[match.end() : next_match.start()]
        compact_gap = re.sub(r"\s+", "", gap)
        if len(compact_gap) <= TIGHT_QA_GAP_LIMIT:
            count += 1
    return count


def _short_dialogue_density(quotes: list[str]) -> float:
    if not quotes:
        return 0.0
    short_count = sum(
        1
        for quote in quotes
        if len(_normalize_dialogue_content(quote)) <= SHORT_SENT_LIMIT
    )
    return _round_rate(short_count / len(quotes))


def _short_paragraph_density(paragraphs: list[str]) -> float:
    if not paragraphs:
        return 0.0
    short_count = sum(
        1
        for paragraph in paragraphs
        if len(re.sub(r"\s+", "", paragraph)) < SHORT_PARAGRAPH_LIMIT
    )
    return _round_rate(short_count / len(paragraphs))


def _count_procedural_exposition_clusters(paragraphs: list[str]) -> int:
    count = 0
    for paragraph in paragraphs:
        compact = re.sub(r"\s+", "", paragraph)
        if len(compact) < 60:
            continue
        term_hits = sum(1 for term in PROCEDURAL_EXPOSITION_TERMS if term in compact)
        flow_hits = sum(1 for marker in PROCEDURAL_FLOW_MARKERS if marker in compact)
        if term_hits >= 3 and term_hits + flow_hits >= 4:
            count += 1
    return count


def _count_story_bible_leakage(paragraphs: list[str]) -> int:
    count = 0
    for paragraph in paragraphs:
        compact = re.sub(r"\s+", "", paragraph or "")
        if not compact:
            continue
        term_hits = sum(compact.count(term) for term in STORY_BIBLE_TERMS)
        if term_hits <= 0:
            continue
        unique_term_hits = sum(1 for term in STORY_BIBLE_TERMS if term in compact)
        has_public_context = any(term in compact for term in STORY_BIBLE_PUBLIC_CONTEXT_TERMS)
        has_dialogue = bool(QUOTE_RE.search(paragraph))
        if unique_term_hits >= 3:
            count += 1
        elif term_hits >= 2 and (has_dialogue or has_public_context):
            count += 1
        elif term_hits >= 1 and has_dialogue and has_public_context:
            count += 1
    return count



_STRUCTURE_LABEL = r"(?:第\s*[0-9一二三四五六七八九十百千零两〇]+\s*[章卷]|\[(?:CH|VOL)-\d+\])"
_STRUCTURE_HEADING = re.compile(rf"^(?:#{{1,6}}\s*)?{_STRUCTURE_LABEL}(?:[ \t:：、].{{0,60}})?$")
_STRUCTURE_END = re.compile(r"^(?:(?:[（(【]\s*)?(?:本章开始|本章完|本章结束|下章预告|未完待续)(?:\s*[）)】])?[。.!！]?)+$")


def is_output_scaffolding_line(line: str) -> bool:
    """Recognize standalone labels, not narrative discussion of book chapters."""
    stripped = line.strip()
    if _STRUCTURE_END.fullmatch(stripped):
        return True
    # A full narrative sentence beginning with a chapter reference is not a heading.
    return not any(ch in stripped for ch in "。！？!?“”‘’「」『』") and bool(_STRUCTURE_HEADING.fullmatch(stripped))


def output_scaffolding_line_indices(text: str) -> set[int]:
    """Do not treat a multiline quotation's contents as model scaffolding."""
    found: set[int] = set()
    stack: list[str] = []
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』"}
    for index, line in enumerate((text or "").splitlines()):
        if not stack and is_output_scaffolding_line(line):
            found.add(index)
        for char in line:
            if stack and char == stack[-1]:
                stack.pop()
            elif char in pairs:
                stack.append(pairs[char])
            elif char == '"':
                stack.append('"')
    return found


def _count_meta_structure_leakage(text: str) -> int:
    return len(output_scaffolding_line_indices(text))


def _directional_listing_stats(paragraph: str) -> tuple[int, set[str]]:
    total = 0
    groups: set[str] = set()
    for group, patterns in _DIRECTIONAL_LISTING_GROUPS.items():
        group_hits = 0
        for pattern in patterns:
            group_hits += len(re.findall(pattern, paragraph or ""))
        if group_hits:
            total += group_hits
            groups.add(group)
    return total, groups


def _count_directional_listings(paragraphs: list[str]) -> int:
    count = 0
    for paragraph in paragraphs:
        hits, groups = _directional_listing_stats(paragraph)
        if hits <= 0:
            continue
        has_env_cue = any(cue in paragraph for cue in _DIRECTIONAL_LISTING_ENV_CUES)
        has_opposing_pair = any(
            left in groups and right in groups
            for left, right in _DIRECTIONAL_LISTING_OPPOSITE_PAIRS
        )
        if hits >= 4 or (hits >= 3 and len(groups) >= 2) or (
            hits >= 2 and has_opposing_pair and has_env_cue
        ):
            count += 1
    return count


def _count_mundane_logic_violations(paragraphs: list[str]) -> int:
    count = 0
    for paragraph in paragraphs:
        compact = re.sub(r"\s+", "", paragraph or "")
        if not compact:
            continue
        has_stall_context = any(term in compact for term in ("烤肠摊", "摊位", "烤肠", "纸箱", "汤锅"))
        if has_stall_context:
            count += sum(1 for term in ("临期面包", "热水", "汤锅") if term in compact)
        if re.search(r"(?:18|十八)岁.{0,18}未成年不行|未成年不行", compact):
            count += 1
        if re.search(
            r"(?:白开水.{0,12}[一二三四五六七八九十两0-9]+块|[一二三四五六七八九十两0-9]+块.{0,12}白开水)",
            compact,
        ):
            count += 1
        if "便利店" in compact:
            count += sum(1 for term in ("消毒水味", "温水") if term in compact)
        if "院子" in compact and re.search(r"围.{0,8}电视", compact):
            count += 1
        if "围着没有声音的电视" in compact:
            count += 1
    return count


def _count_hardship_stacks(paragraphs: list[str]) -> int:
    count = 0
    for idx, paragraph in enumerate(paragraphs, 1):
        compact = re.sub(r"\s+", "", paragraph or "")
        if not compact:
            continue
        hits = sum(1 for term in HARDSHIP_STACK_TERMS if term in compact)
        if hits >= 4 or (idx <= 4 and hits >= 2):
            count += 1
    return count


def _normalize_dialogue_content(text: str) -> str:
    return re.sub(r"[\s，。！？!?；;、,.…—-]+", "", text or "")


def _short_dialogue_runs(text: str) -> list[list[str]]:
    runs: list[list[str]] = []
    current: list[str] = []
    last_end = 0
    for match in QUOTE_RE.finditer(text or ""):
        between = (text or "")[last_end:match.start()]
        if between.strip() and current:
            runs.append(current)
            current = []
        raw = match.group(1)
        normalized = _normalize_dialogue_content(raw)
        if normalized and len(normalized) <= SHORT_DIALOGUE_LIMIT:
            current.append(normalized)
        else:
            if current:
                runs.append(current)
            current = []
        last_end = match.end()
    if (text or "")[last_end:].strip() and current:
        runs.append(current)
    if current:
        runs.append(current)
    return runs


def _count_dialogue_symmetry_risks(text: str) -> int:
    return sum(1 for run in _short_dialogue_runs(text) if len(run) >= DIALOGUE_SYMMETRY_RUN_LIMIT)


def _count_duplicate_short_dialogue_ladders(text: str) -> int:
    windows: Counter[tuple[str, ...]] = Counter()
    for run in _short_dialogue_runs(text):
        if len(run) < 4:
            continue
        for idx in range(0, len(run) - 3):
            windows[tuple(run[idx : idx + 4])] += 1
    return sum(1 for _, count in windows.items() if count > 1)


def _count_perfect_comeback_runs(text: str) -> int:
    count = 0
    for run in _short_dialogue_runs(text):
        if len(run) < PERFECT_COMEBACK_RUN_LIMIT:
            continue
        joined = "".join(run)
        cue_hits = sum(1 for pattern in PERFECT_COMEBACK_CUES if re.search(pattern, joined))
        if cue_hits >= PERFECT_COMEBACK_CUE_LIMIT:
            count += 1
    return count


def _count_duplicate_explanation_spans(paragraphs: list[str]) -> int:
    """Count repeated / aphoristic explanation spans (duplicate_explanation_control).

    Two signals, both sourced from the cross-project prose quality contract:
    standalone 金句/aphorism patterns from the rule catalog, plus a pressure-chain
    explanation that repeats across paragraphs (回去…继续站…解释).
    """
    full = "\n".join(paragraphs)
    # Unique merged span so a single aphorism matched by overlapping catalog
    # patterns (e.g. "所有能解释的退路都在变窄" and "退路.{0,8}变窄") counts once.
    count = _count_unique_regex_hits(full, list(regex_patterns_for("duplicate_explanation_control")))
    pressure_patterns = (
        r"回去.{0,30}(?:赶|挡门|不让|赶出来).{0,50}继续站.{0,50}解释",
        r"继续站.{0,50}解释.{0,50}回去.{0,30}(?:赶|挡门|不让|赶出来)",
    )
    pressure_hits = 0
    for paragraph in paragraphs:
        compact = re.sub(r"\s+", "", paragraph or "")
        if compact and any(re.search(p, compact) for p in pressure_patterns):
            pressure_hits += 1
    if pressure_hits >= 2:
        count += pressure_hits - 1
    return count


# --- Chapter-level anti-padding (paragraph granularity) ---
# Catches "重复解释/金句式心理剖白" at chapter scale, which the sentence-level
# metrics miss: same insight restated paragraph after paragraph, interiority
# monologue stacked with little dialogue/plot advancement.
INTERIORITY_MARKERS = [
    "意识到", "明白", "觉得", "发现", "清楚", "想起", "终于", "忽然", "不再", "知道自己",
    "越来越", "其实", "像是", "仿佛", "这栋楼", "这地方", "让他", "反而", "心里", "原来",
]
INTERIORITY_MIN_MARKERS = 2
INTERIORITY_RATE_LIMIT = 0.45
REPEATED_REALIZATION_RUN_LIMIT = 3
_QUOTE_PRESENCE_RE = re.compile(r"[“\"]")


def _chapter_interiority_metrics(paragraphs: list[str]) -> tuple[float, int, float]:
    """Return (interiority_monologue_rate, repeated_realization_run, dialogue_paragraph_rate).

    An interiority paragraph carries no dialogue and packs >= INTERIORITY_MIN_MARKERS
    introspection markers — a "realization/金句" paragraph. A long consecutive run of
    these is the chapter-level padding signature (each paragraph restates the same
    conclusion instead of advancing).
    """
    if not paragraphs:
        return 0.0, 0, 0.0
    interior_flags: list[bool] = []
    dialogue = 0
    for p in paragraphs:
        has_quote = bool(_QUOTE_PRESENCE_RE.search(p))
        if has_quote:
            dialogue += 1
        is_interior = (
            not has_quote
            and len(p) > 12
            and sum(1 for m in INTERIORITY_MARKERS if m in p) >= INTERIORITY_MIN_MARKERS
        )
        interior_flags.append(is_interior)
    n = len(paragraphs)
    run = max_run = 0
    for flag in interior_flags:
        run = run + 1 if flag else 0
        max_run = max(max_run, run)
    return sum(interior_flags) / n, max_run, dialogue / n


def _micro_action_metrics(text: str) -> tuple[int, float, bool]:
    """Return (beat_count, density_per_1000, overload).

    Overload needs both an absolute floor (MICRO_ACTION_MIN_HITS, so a single
    beat in a short snippet never flags) and a density above
    MICRO_ACTION_DENSITY_LIMIT per 1000 chars.
    """
    beats = _count_unique_regex_hits(text, MICRO_ACTION_BEAT_PATTERNS)
    length = len(re.sub(r"\s+", "", text or ""))
    density = _round_rate(beats * 1000.0 / length) if length else 0.0
    overload = beats >= MICRO_ACTION_MIN_HITS and density > MICRO_ACTION_DENSITY_LIMIT
    return beats, density, overload


def _repeated_rare_phrases(text: str) -> dict[str, int]:
    """Deterministic CJK n-gram tic detector.

    Counts 4-6 char n-grams inside contiguous Han runs, skips any gram touching
    the function-char guard set, keeps grams recurring
    REPEATED_RARE_PHRASE_MIN_COUNT+ times, then reports only maximal phrases
    (a 4-gram inside an equally frequent longer offender is dropped).
    """
    runs = _CJK_RUN_RE.findall(text or "")
    counts: Counter[str] = Counter()
    for run in runs:
        for n in _RARE_PHRASE_NGRAM_SIZES:
            for i in range(len(run) - n + 1):
                gram = run[i : i + n]
                if _RARE_PHRASE_GUARD_CHARS.intersection(gram):
                    continue
                counts[gram] += 1
    offenders = {g: c for g, c in counts.items() if c >= REPEATED_RARE_PHRASE_MIN_COUNT}
    maximal = {
        gram: count
        for gram, count in offenders.items()
        if not any(
            gram != other and gram in other and other_count >= count
            for other, other_count in offenders.items()
        )
    }
    top = sorted(maximal.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    return dict(top)


def analyze_chinese_prose_mechanics(
    text: str, *, genre_hint: str | None = None
) -> ChineseProseMechanicsReport:
    text = text or ""
    report = ChineseProseMechanicsReport()
    sents = _sentences(text)
    paragraphs = _paragraphs(text)
    quotes_all = _quote_contents(text)
    report.paragraph_count = len(paragraphs)
    report.quote_count = len(quotes_all)
    report.sentence_count = len(sents)
    report.short_sentence_run_max, report.short_sentence_runs_over_target = _short_run_metrics(sents)
    forbidden = Counter({w: text.count(w) for w in FORBIDDEN_COLLOCATIONS if text.count(w)})
    report.forbidden_collocation_counts = dict(forbidden)
    report.forbidden_collocation_count = sum(forbidden.values())
    report.space_watchlist_hits = sum(text.count(w) for w in SPACE_WATCHLIST)
    report.verb_watchlist_hits = sum(text.count(w) for w in VERB_WATCHLIST)
    for idx, p in enumerate(paragraphs, 1):
        ex_hits = sum(p.count(w) for w in EXPOSITION_TERMS)
        if ex_hits >= 8 and len(p) > 220:
            report.exposition_cluster_risk += 1
            report.paragraph_risks.append(f"paragraph_{idx}: exposition_cluster")
    quotes = re.findall(r"[“\"]([^”\"]{81,})[”\"]", text)
    report.long_quote_segments_gt80 = len(quotes)
    seen = Counter(q for q in re.findall(r"[“\"]([^”\"]{20,})[”\"]", text))
    report.duplicate_quote_segments_gt20 = sum(1 for _, n in seen.items() if n > 1)
    report.action_dialogue_beat_count = _count_action_dialogue_beats(text)
    report.prop_fiddling_count = _count_unique_regex_hits(text, PROP_FIDDLING_PATTERNS)
    report.explicit_pause_marker_count = sum(text.count(marker) for marker in EXPLICIT_PAUSE_MARKERS)
    report.direct_intent_exposition_count = _count_unique_regex_hits(
        text, DIRECT_INTENT_EXPOSITION_PATTERNS
    )
    report.motive_exposition_count = _count_unique_regex_hits(text, MOTIVE_EXPOSITION_PATTERNS)
    report.cheap_wit_count = _count_unique_regex_hits(text, CHEAP_WIT_PATTERNS)
    report.dialogue_symmetry_risk_count = _count_dialogue_symmetry_risks(text)
    report.perfect_comeback_run_count = _count_perfect_comeback_runs(text)
    report.duplicate_short_dialogue_ladder_count = _count_duplicate_short_dialogue_ladders(text)
    report.spatial_mapping_count = _count_unique_regex_hits(text, STATIC_SPATIAL_MAPPING_PATTERNS)
    report.biographical_infodump_count = _count_unique_regex_hits(text, BIOGRAPHICAL_INFODUMP_PATTERNS)
    (
        report.action_quote_paragraph_count,
        report.action_quote_paragraph_rate,
    ) = _action_quote_paragraph_metrics(paragraphs)
    report.tight_qa_pair_count = _count_tight_qa_pairs(text)
    report.short_dialogue_density = _short_dialogue_density(quotes_all)
    report.short_paragraph_density = _short_paragraph_density(paragraphs)
    report.procedural_exposition_cluster_count = _count_procedural_exposition_clusters(paragraphs)
    report.story_bible_leakage_count = _count_story_bible_leakage(paragraphs)
    report.meta_structure_leakage_count = _count_meta_structure_leakage(text)
    report.directional_listing_count = _count_directional_listings(paragraphs)
    report.awkward_register_count = _count_unique_regex_hits(text, AWKWARD_REGISTER_PATTERNS)
    report.limited_pov_leak_count = _count_unique_regex_hits(text, LIMITED_POV_LEAK_PATTERNS)
    report.mundane_logic_violation_count = _count_mundane_logic_violations(paragraphs)
    report.hardship_stack_count = _count_hardship_stacks(paragraphs)
    report.resource_continuity_count = _count_unique_regex_hits(text, RESOURCE_CONTINUITY_PATTERNS)
    report.mundane_register_count = _count_unique_regex_hits(text, MUNDANE_REGISTER_PATTERNS)
    report.action_causality_count = _count_unique_regex_hits(text, ACTION_CAUSALITY_PATTERNS)
    report.motivation_gap_count = _count_unique_regex_hits(text, MOTIVATION_GAP_PATTERNS)
    report.scene_plausibility_count = _count_unique_regex_hits(text, SCENE_PLAUSIBILITY_PATTERNS)
    report.transport_logic_count = _count_unique_regex_hits(text, TRANSPORT_LOGIC_PATTERNS)
    report.semantic_collocation_count = _count_unique_regex_hits(text, SEMANTIC_COLLOCATION_PATTERNS)
    report.shelter_cost_logic_count = _count_unique_regex_hits(text, SHELTER_COST_LOGIC_PATTERNS)
    report.abstract_evasion_count = _count_unique_regex_hits(text, ABSTRACT_EVASION_PATTERNS)
    report.pseudo_literary_register_count = _count_unique_regex_hits(
        text, PSEUDO_LITERARY_REGISTER_PATTERNS
    )
    report.duplicate_explanation_span_count = _count_duplicate_explanation_spans(paragraphs)
    (
        report.micro_action_beat_count,
        report.micro_action_density_per_1000,
        report.micro_action_overload,
    ) = _micro_action_metrics(text)
    report.repeated_rare_phrases = _repeated_rare_phrases(text)
    report.repeated_rare_phrase_count = len(report.repeated_rare_phrases)
    report.translationese_marker_count = _count_unique_regex_hits(
        text, TRANSLATIONESE_MARKER_PATTERNS
    )
    # Era/register and seam statistics remain observations, not rewrite scores.
    # The era detector only activates when genre_hint classifies unambiguously.
    report.era_register_class = classify_genre_register(genre_hint)
    era_counts = _era_register_conflicts(text, report.era_register_class)
    report.era_register_conflict_count = sum(era_counts.values())
    report.era_register_conflicts = dict(
        sorted(era_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:ERA_REGISTER_TOP_TERMS]
    )
    report.seam_duplication_count, report.seam_duplication_pairs = _seam_duplication(text)
    report.floating_fragment_count, report.floating_fragment_samples = _floating_fragments(text)
    # Aggregate "normal modern Chinese" violation so the gate/UI catch the family
    # of problems instead of one-off phrases (cross_project_prose_quality_contract).
    # Count by unique merged span across the families so a phrase matching more than
    # one family (e.g. semantic + abstract) is not double-counted in the audit number.
    report.plain_contemporary_violation_count = _count_unique_regex_hits(
        text,
        PSEUDO_LITERARY_REGISTER_PATTERNS
        + SEMANTIC_COLLOCATION_PATTERNS
        + AWKWARD_REGISTER_PATTERNS
        + MUNDANE_REGISTER_PATTERNS
        + ABSTRACT_EVASION_PATTERNS,
    )
    (
        report.interiority_monologue_rate,
        report.repeated_realization_run,
        report.dialogue_paragraph_rate,
    ) = _chapter_interiority_metrics(paragraphs)
    # These detectors have no project facts or POV contract. Counts describe
    # form or flag passages for reading; they cannot establish literary defects.
    # Only explicit standalone output scaffolding blocks this mechanical stage.
    report.passed = report.meta_structure_leakage_count == 0
    return report


def analyze_to_safe_dict(text: str, *, genre_hint: str | None = None) -> dict[str, Any]:
    return analyze_chinese_prose_mechanics(text, genre_hint=genre_hint).to_safe_dict()


def dumps_report(report: ChineseProseMechanicsReport | dict[str, Any]) -> str:
    safe = report.to_safe_dict() if hasattr(report, "to_safe_dict") else dict(report)
    return json.dumps(safe, ensure_ascii=False, indent=2)


# --- Recurring-defect ledger (docs/DEFECT_GOVERNANCE.md) ---
# The general immunization loop: the system learns per-project from its own
# evaluation history without human intervention. Any evaluator violation-type
# tag recurring in >= RECURRING_DEFECT_MIN_CHAPTERS of the last N evaluated
# chapters is surfaced as a 【本项目高发问题】 block in the generation preflight,
# carrying the tag's repair_action from QUALITY_GATE_RULES plus one concrete
# recent example. Fully deterministic — no LLM involved.
RECURRING_DEFECT_MIN_CHAPTERS = 2
RECURRING_DEFECT_MAX_TAGS = 6
RECURRING_DEFECT_EXAMPLE_MAX_CHARS = 80
_RECURRING_DEFECT_FALLBACK_REPAIR = "先归因到规则族并补支撑/降级结果，不得只修单句。"


def aggregate_recurring_defects(
    issue_history: list[list[dict[str, Any]]] | None,
    *,
    min_chapters: int = RECURRING_DEFECT_MIN_CHAPTERS,
) -> dict[str, dict[str, Any]]:
    """Aggregate recent chapters' evaluator issues into a recurrence ledger.

    ``issue_history`` holds one ``issues_json`` list per recent chapter (newest
    first). Returns ``{tag: {"chapters": n, "repair_action": str, "example":
    str}}`` for tags seen in at least ``min_chapters`` distinct chapters,
    strongest recurrence first, capped at RECURRING_DEFECT_MAX_TAGS.
    """
    from app.services.narrative_quality_gates import QUALITY_GATE_RULES, issue_violation_type

    chapters_per_tag: Counter[str] = Counter()
    example_per_tag: dict[str, str] = {}
    for chapter_issues in issue_history or []:
        seen_in_chapter: set[str] = set()
        for issue in chapter_issues or []:
            if not isinstance(issue, dict):
                continue
            tag = issue_violation_type(issue)
            if not tag:
                continue
            if tag not in seen_in_chapter:
                seen_in_chapter.add(tag)
                chapters_per_tag[tag] += 1
            if tag not in example_per_tag:
                desc = str(issue.get("description") or issue.get("suggestion") or "").strip()
                if desc:
                    example_per_tag[tag] = desc[:RECURRING_DEFECT_EXAMPLE_MAX_CHARS]
    recurring = sorted(
        ((tag, count) for tag, count in chapters_per_tag.items() if count >= max(1, min_chapters)),
        key=lambda kv: (-kv[1], kv[0]),
    )[:RECURRING_DEFECT_MAX_TAGS]
    result: dict[str, dict[str, Any]] = {}
    for tag, count in recurring:
        rule = QUALITY_GATE_RULES.get(tag)
        result[tag] = {
            "chapters": count,
            "repair_action": rule.repair_action if rule else _RECURRING_DEFECT_FALLBACK_REPAIR,
            "example": example_per_tag.get(tag, ""),
        }
    return result


def render_recurring_defect_block(recurring: dict[str, dict[str, Any]]) -> str:
    if not recurring:
        return ""
    lines = ["\n【本项目高发问题（最近数章重复出现，写正文前必须优先规避）】"]
    for tag, info in recurring.items():
        example = str(info.get("example") or "")
        example_part = f" 最近示例：{example}" if example else ""
        lines.append(
            f"- {tag}（最近评审中 {int(info.get('chapters') or 0)} 章复发）："
            f"修复：{info.get('repair_action') or ''}{example_part}"
        )
    return "\n".join(lines)


def build_generation_preflight_prompt(
    report: ChineseProseMechanicsReport | dict[str, Any] | None,
    *,
    issue_focus: list[str] | None = None,
    version_label: str = "v4.36",
    recurring_issue_history: list[list[dict[str, Any]]] | None = None,
    recurring_min_chapters: int = RECURRING_DEFECT_MIN_CHAPTERS,
) -> str:
    safe = report.to_safe_dict() if hasattr(report, "to_safe_dict") else (report or {})
    recurring_block = render_recurring_defect_block(
        aggregate_recurring_defects(recurring_issue_history, min_chapters=recurring_min_chapters)
    )
    from app.services.narrative_quality_gates import CHINESE_PROSE_MECHANICS_PROMPT

    focus = "、".join(issue_focus or [])
    return (
        f"\n\n【{version_label} 行文观察：chinese_prose_mechanics_observation】\n"
        "以下历史统计只供定位和阅读，不证明当前正文有错，不要求清零或达到比例。"
        "不能因为短句、停顿、回忆、长对白、重复词语或心理描写的次数改写正文。\n"
        + json.dumps(safe, ensure_ascii=False)
        + (f"\n待结合原文核查的线索：{focus}" if focus else "")
        + "\n" + CHINESE_PROSE_MECHANICS_PROMPT
        + "\n历史评审也可能误报；先核对当前原文和作者要求，只有确认问题仍存在才调整。"
        + recurring_block
    )
