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
    "供纸案匣", "外目三", "手腕一翻", "从肘下钻过", "半寸", "半息", "半指",
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
    procedural_exposition_cluster_count: int = 0
    story_bible_leakage_count: int = 0
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
    paragraph_risks: list[str] = field(default_factory=list)

    @property
    def pass_(self) -> bool:
        return self.passed

    def to_safe_dict(self) -> dict[str, Any]:
        return {
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
            "procedural_exposition_cluster_count": self.procedural_exposition_cluster_count,
            "story_bible_leakage_count": self.story_bible_leakage_count,
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
    count = 0
    for pattern in regex_patterns_for("duplicate_explanation_control"):
        if re.search(pattern, full):
            count += 1
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


def analyze_chinese_prose_mechanics(text: str) -> ChineseProseMechanicsReport:
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
    # Aggregate "normal modern Chinese" violation so the gate/UI catch the family
    # of problems instead of one-off phrases (cross_project_prose_quality_contract).
    report.plain_contemporary_violation_count = (
        report.pseudo_literary_register_count
        + report.semantic_collocation_count
        + report.awkward_register_count
        + report.mundane_register_count
        + report.abstract_evasion_count
    )
    report.passed = not (
        report.forbidden_collocation_count
        or report.space_watchlist_hits
        or report.short_sentence_run_max > 8
        or report.short_sentence_runs_over_target > 8
        or report.exposition_cluster_risk
        or report.long_quote_segments_gt80
        or report.duplicate_quote_segments_gt20
        or report.action_dialogue_beat_count > ACTION_DIALOGUE_BEAT_LIMIT
        or report.prop_fiddling_count
        or report.explicit_pause_marker_count
        or report.direct_intent_exposition_count
        or report.motive_exposition_count
        or report.cheap_wit_count
        or report.dialogue_symmetry_risk_count
        or report.perfect_comeback_run_count
        or report.duplicate_short_dialogue_ladder_count
        or report.spatial_mapping_count
        or report.biographical_infodump_count
        or (
            report.action_quote_paragraph_count >= ACTION_QUOTE_PARAGRAPH_MIN_COUNT
            and report.action_quote_paragraph_rate > ACTION_QUOTE_PARAGRAPH_RATE_LIMIT
        )
        or report.tight_qa_pair_count > TIGHT_QA_PAIR_LIMIT
        or (
            report.quote_count >= SHORT_DIALOGUE_DENSITY_MIN_QUOTES
            and report.short_dialogue_density > SHORT_DIALOGUE_DENSITY_LIMIT
        )
        or (
            report.paragraph_count >= SHORT_PARAGRAPH_DENSITY_MIN_PARAGRAPHS
            and report.short_paragraph_density > SHORT_PARAGRAPH_DENSITY_LIMIT
        )
        or report.procedural_exposition_cluster_count
        or report.story_bible_leakage_count
        or report.directional_listing_count
        or report.awkward_register_count
        or report.limited_pov_leak_count
        or report.mundane_logic_violation_count
        or report.hardship_stack_count
        or report.resource_continuity_count
        or report.mundane_register_count
        or report.action_causality_count
        or report.motivation_gap_count
        or report.scene_plausibility_count
        or report.transport_logic_count
        or report.semantic_collocation_count
        or report.shelter_cost_logic_count
        or report.abstract_evasion_count
        or report.pseudo_literary_register_count
        or report.plain_contemporary_violation_count
        or report.duplicate_explanation_span_count
        or report.paragraph_risks
    )
    return report


def analyze_to_safe_dict(text: str) -> dict[str, Any]:
    return analyze_chinese_prose_mechanics(text).to_safe_dict()


def dumps_report(report: ChineseProseMechanicsReport | dict[str, Any]) -> str:
    safe = report.to_safe_dict() if hasattr(report, "to_safe_dict") else dict(report)
    return json.dumps(safe, ensure_ascii=False, indent=2)


def build_generation_preflight_prompt(
    report: ChineseProseMechanicsReport | dict[str, Any] | None,
    *,
    issue_focus: list[str] | None = None,
    version_label: str = "v4.36",
) -> str:
    safe = report.to_safe_dict() if hasattr(report, "to_safe_dict") else (report or {})
    current_short_runs = int(safe.get("short_sentence_runs_over_target") or 0)
    current_short_run_max = int(safe.get("short_sentence_run_max") or 0)
    forbidden = int(safe.get("forbidden_collocation_count") or 0)
    space = int(safe.get("space_watchlist_hits") or 0)
    action_dialogue = int(safe.get("action_dialogue_beat_count") or 0)
    prop_fiddling = int(safe.get("prop_fiddling_count") or 0)
    pause_markers = int(safe.get("explicit_pause_marker_count") or 0)
    direct_intent = int(safe.get("direct_intent_exposition_count") or 0)
    motive_exposition = int(safe.get("motive_exposition_count") or 0)
    cheap_wit = int(safe.get("cheap_wit_count") or 0)
    dialogue_symmetry = int(safe.get("dialogue_symmetry_risk_count") or 0)
    perfect_comeback = int(safe.get("perfect_comeback_run_count") or 0)
    duplicate_dialogue = int(safe.get("duplicate_short_dialogue_ladder_count") or 0)
    spatial_mapping = int(safe.get("spatial_mapping_count") or 0)
    biographical_infodump = int(safe.get("biographical_infodump_count") or 0)
    action_quote_paragraph = int(safe.get("action_quote_paragraph_count") or 0)
    action_quote_rate = float(safe.get("action_quote_paragraph_rate") or 0.0)
    tight_qa = int(safe.get("tight_qa_pair_count") or 0)
    short_dialogue_density = float(safe.get("short_dialogue_density") or 0.0)
    short_paragraph_density = float(safe.get("short_paragraph_density") or 0.0)
    procedural_clusters = int(safe.get("procedural_exposition_cluster_count") or 0)
    story_bible_leakage = int(safe.get("story_bible_leakage_count") or 0)
    directional_listing = int(safe.get("directional_listing_count") or 0)
    awkward_register = int(safe.get("awkward_register_count") or 0)
    limited_pov_leak = int(safe.get("limited_pov_leak_count") or 0)
    mundane_logic = int(safe.get("mundane_logic_violation_count") or 0)
    hardship_stack = int(safe.get("hardship_stack_count") or 0)
    resource_continuity = int(safe.get("resource_continuity_count") or 0)
    mundane_register = int(safe.get("mundane_register_count") or 0)
    action_causality = int(safe.get("action_causality_count") or 0)
    motivation_gap = int(safe.get("motivation_gap_count") or 0)
    scene_plausibility = int(safe.get("scene_plausibility_count") or 0)
    transport_logic = int(safe.get("transport_logic_count") or 0)
    semantic_collocation = int(safe.get("semantic_collocation_count") or 0)
    shelter_cost_logic = int(safe.get("shelter_cost_logic_count") or 0)
    abstract_evasion = int(safe.get("abstract_evasion_count") or 0)
    pseudo_literary_register = int(safe.get("pseudo_literary_register_count") or 0)
    focus = "、".join(issue_focus or []) or "短句链、动作切片、空间刻度、生造名词、解释堆叠、对话节拍器、道具摆弄、显性停顿、潜台词说破、排比问答、物理测绘、履历自白、廉价机智、接梗强迫症、动机交底、设定名泄漏、方位导览罗列、生活逻辑失真、书面腔用词、伪文学压缩腔、有限视角越界、苦难标签堆叠、资源连续性矛盾、动作因果不成立、入门动机缺桥、年龄逻辑、抽象解释、白开水定价"
    return (
        f"\n\n【{version_label} 生成前自检：chinese_prose_mechanics_observation】"
        f"\n当前机械指标：short_sentence_run_max={current_short_run_max}, "
        f"short_sentence_runs_over_target={current_short_runs}, forbidden={forbidden}, "
        f"space={space}, action_dialogue={action_dialogue}, "
        f"prop_fiddling={prop_fiddling}, pause_markers={pause_markers}, "
        f"direct_intent={direct_intent}, motive_exposition={motive_exposition}, cheap_wit={cheap_wit}, "
        f"dialogue_symmetry={dialogue_symmetry}, perfect_comeback={perfect_comeback}, "
        f"duplicate_dialogue={duplicate_dialogue}, spatial_mapping={spatial_mapping}, "
        f"biographical_infodump={biographical_infodump}, "
        f"action_quote_paragraph={action_quote_paragraph}, action_quote_rate={action_quote_rate:.3f}, "
        f"tight_qa={tight_qa}, short_dialogue_density={short_dialogue_density:.3f}, "
        f"short_paragraph_density={short_paragraph_density:.3f}, procedural_clusters={procedural_clusters}, "
        f"story_bible_leakage={story_bible_leakage}, directional_listing={directional_listing}, "
        f"awkward_register={awkward_register}, limited_pov_leak={limited_pov_leak}, "
        f"mundane_logic={mundane_logic}, hardship_stack={hardship_stack}, "
        f"resource_continuity={resource_continuity}, mundane_register={mundane_register}, "
        f"action_causality={action_causality}, motivation_gap={motivation_gap}, "
        f"scene_plausibility={scene_plausibility}, transport_logic={transport_logic}, "
        f"semantic_collocation={semantic_collocation}, shelter_cost_logic={shelter_cost_logic}, "
        f"abstract_evasion={abstract_evasion}, pseudo_literary_register={pseudo_literary_register}."
        f"\n关注项：{focus}."
        "\nv4.38 修正：v4.36 能清掉动作+台词、道具摆弄和显性停顿，但仍会复发排比短问答、静态坐标测绘、NPC 式履历自白、廉价机智和动机交底。本轮先破坏模型安全路径：不要用整齐短句梯子模拟高压交锋，不要用精确距离词给人物测绘站位，不要在对峙中按年龄或时间轴背设定，也不要把“踩烂了，算你买 / 先问问它算谁卖”这类抛梗塞进日常对白。"
        "尤其不要复现“说好一行 / 就一行 / 多看一个字呢 / 你自己合上 / 认错呢 / 认对呢 / 明早我带见证到市书会认旧物”这一整组口诀式对仗；这类回合必须改成证物打断、抢白、避答或直接抛结论。"
        "age_plausibility 必须执行：十八岁去旅馆不要写成未成年不行，更自然的阻力是满房、押金、证件、前台态度和关门时间。"
        "abstract_reasoning_zero 必须执行：删除试错、本质上、底层逻辑、慢慢试错这类元语言解释，把原因落到余额、体力、时间、电量、路况和退路。"
        "门缝干燥或灯光稳定都不是进门许可；必须先有退路收窄、明确求助或现实压力把选择压死。"
        "返回前必须先做词汇清零自检：半寸、半息、半指、寸许、尺许、半尺、半丈、几步、几尺、几丈、几寸、一尺、一丈、三步外全部为 0，腕、膝、踝、贴着也尽量不用；半步只允许用于逼近/退开等动态压迫，不允许写成静态坐标。随后做节奏合并：目标约 4000 字，合理范围 2000-6000；不要为凑字数补空话、金句、制度解释或重复心理剖白。连续短句不得超过 4 句，用中句承接因果、环境变化和受击反应，禁止把动作拆成碎片化短句。"
        "exposition_cluster_risk 必须为 0；守备、门人、供奉等职能词禁止在同段密集复现，必要时合并为一处交代，其余用指代、位置、声响、受击反应承接。动作只写意图和结果，不写逐帧轨迹。"
        "对话段必须执行 floating_dialogue_exchange：逼问、压价、试探时允许连续纯台词交替，不给每句台词配动作。"
        "dialogue_symmetry_risk_count 必须为 0；严禁连续四组以上短问短答，禁止镜像句式复述。必须加入答非所问、抢白、动作打断或直接抛结论来破坏对称性。"
        "perfect_comeback_run_count 必须为 0；不许让连续多轮短促问答像排练好的接梗梯子一样一路顺滑落地，信息要有掉地、岔开、迟钝或抢白。"
        "duplicate_short_dialogue_ladder_count 必须为 0；同一组短对白梯子不得重复出现，尤其禁止看一行/认错呢/认对呢这类排比问答复现。"
        "prop_fiddling_count 必须为 0；拨算盘、绕细绳、擦砚台、摸杯子、挪纸张等道具动作若不承担阻挡、掩饰、转移证物或爆发情绪就删除。"
        "explicit_pause_marker_count 必须为 0；禁用安静了一会儿、沉默了、没有立刻回话、一小会儿、半晌、顿了顿、停了一下。"
        "cheap_wit_count 必须为 0；禁用踩烂了算你买、算谁卖、You break it, you buy it 这类廉价机智或翻译腔抛梗。"
        "direct_intent_exposition_count 与 motive_exposition_count 必须为 0；禁用你怕我、你想让、我想让你、你其实、你就是想、你无非是、赖账、本质上、底层逻辑等直接拆穿意图的句式，用反问、避答、压价和局部事实保留潜台词。"
        "spatial_mapping_count 必须为 0；禁用三步外、一指宽、影子外、几寸、一尺、一丈、几步、几尺、几丈等静态测绘词。人物位置只能用逼近、退开、让开、挡住、压住去表达趋势或遮挡。"
        "biographical_infodump_count 必须为 0；角色用往事作证时最多两句，不许从几岁讲到几岁，不许按时间轴背履历。事实必须直接切入当前冲突。"
        "story_bible_leakage_count 必须为 0；隐藏世界不能通过广告、海报、新闻、路人闲聊或旁白一次性列出设定词。"
        "setting_name_dialogue_zero 必须执行；路人、新闻、店员、邻居、广告、海报和闲聊不得字正腔圆讨论核心世界观名词。"
        "超自然影响必须降维成封路、停电、绕路、物价、黑车、查得紧、上面、那帮人、那种事、清道等生活影响和代词。"
        "不要把神脉、学院、执行者、等级、能力名、终局谜团等世界观说明书塞进正文道具；POV 不知道的专名只能以异常痕迹、误听片段或后续解释出现。"
        "directional_listing_count 必须为 0；directional_listing_zero 必须执行。环境描写禁止左边/右边/东头/西头/前后等导览式罗列，只抓一个与氛围或剧情冲突的核心反差点。"
        "mundane_scene_plausibility 必须执行；街边摊、便利店、保安、食堂、房东催租等日常场景必须符合真实生活经验，烤肠摊不要顺手卖临期面包、汤锅或热水，便利店不要硬塞消毒水味和温水，雨夜院子里不要安排人群围电视。"
        "plain_modern_register 必须执行；能用正常现代汉语就用正常说法，写“锁屏/按灭屏幕”“别挡着锅”“别往楼里跑/别进去”，不要写“把手机按黑”“别挡锅”“别碰那边”“带了急”这类别扭压缩词。"
        "plain_contemporary_chinese 必须执行；现代日常场景用完整、自然、普通的现代汉语，不要为了显得干练或有文学感压缩句子。写“他叫了一声：‘师傅。’”“值守员抬头：‘干嘛？’”，不要写“喊了声师傅”“来意说得很低”“终于抬头”这类伪文学压缩腔。"
        "limited_pov_only 必须执行；第三人称有限视角只写主角能看见、听见、感觉到的事实，不写食堂阿姨才想起他、没人记得他、没人问他这类替别人下心理结论的旁白。"
        "semantic_density_budget 必须执行；开篇边缘化和苦难感只保留两三个与当前困境直接相关的物理痛点，不要把老师、门卫、班群、食堂、房东等同质信息堆成标签清单。"
        "resource_continuity 必须执行；钱、手机、电量、支付方式、交通选择要前后一致。能在手机上叫车就说明存在账户/移动支付语境，不能同时写成只剩口袋零钱还准备坐车；附近无车也不能让旁人立刻坐上同类出租车而不解释差异。"
        "action_causality 必须执行；动作与结果必须能成立。不要写挡着锅却雨水进锅、香味直接导致胃更空、看见灯就自然进陌生门这类因果跳跃。"
        "motivation_bridge 必须执行；角色进入陌生建筑、异常门、私人空间前必须有足够压力、退路关闭、误判理由、求生诱因或外部催逼，不能只因看到一线光就进门。"
        "scene_plausibility_count 必须为 0；普通路人、店员、摊主、保安的行为必须符合当下资源和场景，不为主角提供便利巧合或制造自相矛盾的对照。"
        "transport_logic_count 必须为 0；交通链路必须成立，末班车已经没了就不要再写最近换乘赶不上，改成夜班线路、步行距离、封路、叫车失败或余额不足等具体阻力。"
        "semantic_collocation_count 必须为 0；现代汉语搭配必须完整自然。写透不出灯光/没有灯光，不写透不出灯；写价格超过余额/房费太贵，不写价格难看；写没有人声/没有开门动静，不写可以借口留下来的声音。"
        "shelter_cost_logic_count 必须为 0；不住旅馆、宾馆或酒店的原因必须落在房费、押金、余额、满房、证件、关门、风险或距离上，不要用背包放干、没力气碰脸色、从头看到脚这类抽象借口替代现实阻力。"
        "abstract_evasion_count 必须为 0；删除试错、借口留下来、价格难看、碰这种脸色等抽象逃避词，把行动原因落回钱、时间、电量、体力、路况和退路。"
        "pseudo_literary_register_count 必须为 0；删除半文言、硬压缩、装腔的现代叙述。普通问路、求助、登记、买东西、躲雨都用普通人会说的完整话。"
        "dialogue_topology_limit 必须执行：连续含引号段落不得超过四段，连续纯短对白不得超过两段，每个场景紧贴问答不得超过三组。"
        "超出时用未答、抢白、误解、环境声、动作结果、证物变化或概括性侧写打断，不要把整章写成问答剧本。"
        "结构拓扑也必须自检：动作对白绑定率不得超过 0.35，不能让多数段落都写成“人物摆一下/挪一下/看一下 + 台词”；"
        "紧贴问答不得超过 12 组，连续追问必须被无视、抢白、岔开、证物或环境打断；"
        "短对白密度不得过半，短句可用于压迫但不能把整章写成剧本回合；"
        "短段落密度不得超过 0.35，必要时合并同一动作或同一证据链；"
        "程序性解释簇必须拆散，旧库、码头、灯籍、封存、回封、待验等信息不能由专家 NPC 一口气讲完，必须由证物和争执分批露出。"
    )
