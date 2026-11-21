"""确定性缺陷检测器(纯函数,不调用模型)。

六类缺陷都来自真实生成稿反复出现的具体问题,不是抽象评分:

1. ``pov_breach``          叙述视角越界:限知视角进入他人内心,或镜头无授权切走。
2. ``instruction_leak``    写作指令/规划字段泄漏进正文。
3. ``rule_recitation``     把设定条文当叙事复述,代替人物经验。
4. ``metaphor_stack``      泛化强度比喻堆叠。
5. ``outcome_unfulfilled`` 本场必须完成的结果没有兑现,或结尾只有打算。
6. ``fact_violation``      必须保持的事实被改动,或新增了明令禁止的元素。

每条 finding 都带 ``quote`` 与 ``offset``,便于回到原文核对。检测器只报"命中",不作
文学判决;编辑式审读负责判断命中是否真的伤害阅读。严重度分两档:

- ``hard``:事实/结果/禁止项。命中基本可以判定为稿件缺陷。
- ``soft``:语言与叙述技艺。命中是待核对的线索,需要人读原句。

约束:同一份正文两次运行,输出逐字一致(findings 按 (category, offset, detector) 排序)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CATEGORIES = (
    "pov_breach",
    "instruction_leak",
    "rule_recitation",
    "metaphor_stack",
    "outcome_unfulfilled",
    "fact_violation",
)

# ---------------------------------------------------------------------------
# 文本切分:偏移必须基于原文,便于回查
# ---------------------------------------------------------------------------

_SENTENCE_RE = re.compile(r"[^。!?;;\n]+[。!?;;]?")


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str


def split_sentences(text: str) -> list[Span]:
    spans: list[Span] = []
    for m in _SENTENCE_RE.finditer(text or ""):
        chunk = m.group(0)
        if chunk.strip():
            spans.append(Span(m.start(), m.end(), chunk))
    return spans


def split_paragraphs(text: str) -> list[Span]:
    spans: list[Span] = []
    cursor = 0
    for raw in re.split(r"(\n+)", text or ""):
        if raw and raw.strip() and not raw.strip("\n") == "":
            if "\n" not in raw:
                spans.append(Span(cursor, cursor + len(raw), raw))
        cursor += len(raw)
    return spans


def _sentence_at(text: str, offset: int) -> str:
    for span in split_sentences(text):
        if span.start <= offset < span.end:
            return span.text.strip()
    return text[max(0, offset - 20): offset + 20].strip()


@dataclass(frozen=True)
class Finding:
    category: str
    detector: str
    severity: str
    quote: str
    offset: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "detector": self.detector,
            "severity": self.severity,
            "quote": self.quote,
            "offset": self.offset,
            "detail": self.detail,
        }


@dataclass
class Detection:
    case_id: str
    chars: int
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    requirements: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        out = {name: 0 for name in CATEGORIES}
        for f in self.findings:
            out[f.category] = out.get(f.category, 0) + 1
        return out

    def hard_counts(self) -> dict[str, int]:
        out = {name: 0 for name in CATEGORIES}
        for f in self.findings:
            if f.severity == "hard":
                out[f.category] = out.get(f.category, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        ordered = sorted(self.findings, key=lambda f: (f.category, f.offset, f.detector, f.quote))
        return {
            "case_id": self.case_id,
            "chars": self.chars,
            "counts": self.counts(),
            "hard_counts": self.hard_counts(),
            "metrics": dict(sorted(self.metrics.items())),
            "requirements": self.requirements,
            "findings": [f.to_dict() for f in ordered],
        }


# ---------------------------------------------------------------------------
# 1. 叙述视角越界
# ---------------------------------------------------------------------------

_INTERIORITY = re.compile(
    r"(?:心里|心中|心底|暗自|暗暗|默默|悄悄想|知道|明白|意识到|想起|记得|以为|"
    r"觉得|感到|后悔|庆幸|盘算|琢磨|打量着自己|想起自己)"
)
_OMNISCIENT_CUES = (
    "与此同时",
    "而在另一边",
    "他不知道的是",
    "她不知道的是",
    "殊不知",
    "同一时刻,",
    "几天后他才知道",
    "这时她还不知道",
)


def _detect_pov_breach(text: str, case: Any) -> list[Finding]:
    findings: list[Finding] = []
    for name in case.non_pov_cast:
        for m in re.finditer(re.escape(name), text):
            tail = text[m.end(): m.end() + 8]
            hit = _INTERIORITY.search(tail)
            if not hit:
                continue
            findings.append(
                Finding(
                    category="pov_breach",
                    detector="interior_of_non_pov",
                    severity="soft",
                    quote=_sentence_at(text, m.start()),
                    offset=m.start(),
                    detail=(
                        f"限知视角为{case.pov_character},此处直接进入{name}的"
                        f"内心/感知({hit.group(0)})"
                    ),
                )
            )
    for cue in _OMNISCIENT_CUES:
        for m in re.finditer(re.escape(cue), text):
            findings.append(
                Finding(
                    category="pov_breach",
                    detector="omniscient_camera_shift",
                    severity="soft",
                    quote=_sentence_at(text, m.start()),
                    offset=m.start(),
                    detail=f"镜头在限知视角之外切换({cue})",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 2. 写作指令 / 规划字段泄漏
# ---------------------------------------------------------------------------

_PLANNING_FIELD_RE = re.compile(
    r"(start_state|continuity_ledger|result_strength|transition_bridge|key_action|"
    r"time_cue|entity_transfers|power_resource_map|information_state|mechanism_limits|"
    r"action_budget|inference_ledger|evidence_strength|"
    r"台账|字段|场景合同|规划草案)"
)
_HARD_LEAK_RE = re.compile(r"(```|\{\s*\"|^\s*#|\*\*[^\*]+\*\*|^\s*[-*]\s+)", re.MULTILINE)
_SOFT_LEAK_PATTERNS = (
    (r"必须用.{0,8}(?:展示|表现|体现)", "把写作任务写进正文"),
    (r"(?:需要|要)在(?:正文|本场|这一场)(?:里|中)?(?:写出|交代|完成)", "把写作任务写进正文"),
    (r"按照?(?:本章)?大纲", "在正文里点出大纲"),
    (r"作者(?:的要求|希望|安排|意图)", "在正文里点出作者"),
    (r"读者(?:会|能|可以)?(?:看到|看到|明白|意识到|发现)", "在正文里点出读者"),
    (r"本场(?:需要|要|应)(?:完成|写到|交代)", "在正文里点出场景任务"),
    (r"如同(?:前面|上文)(?:所述|所说)", "叙述者跳出故事解释结构"),
    (r"伏笔(?:需要|应当|必须|已经埋下)", "在正文里点出伏笔机制"),
    (r"(?:第[一二三四五六七八九十]+章|本章)的?(?:任务|目标是)", "在正文里点出章节任务"),
)


def _detect_instruction_leak(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for m in _PLANNING_FIELD_RE.finditer(text):
        findings.append(
            Finding(
                category="instruction_leak",
                detector="planning_field",
                severity="hard",
                quote=_sentence_at(text, m.start()),
                offset=m.start(),
                detail=f"正文出现规划字段/结构术语:{m.group(0)}",
            )
        )
    for m in _HARD_LEAK_RE.finditer(text):
        token = m.group(0).strip()
        if token in {"-", "*"}:
            continue
        findings.append(
            Finding(
                category="instruction_leak",
                detector="markdown_or_structure",
                severity="hard",
                quote=_sentence_at(text, m.start()),
                offset=m.start(),
                detail=f"正文出现非小说结构标记:{token!r}",
            )
        )
    for pattern, detail in _SOFT_LEAK_PATTERNS:
        for m in re.finditer(pattern, text):
            findings.append(
                Finding(
                    category="instruction_leak",
                    detector="writing_instruction",
                    severity="soft",
                    quote=_sentence_at(text, m.start()),
                    offset=m.start(),
                    detail=detail,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 判据只认"把设定当成说明讲出来"的句式,不认机制词出现或重复本身:
#   1) mechanism_explained:机制词紧邻界限词(只够/不能/必须/一炷香之内……),
#      即把"条件+上限"说成一句规则;
#   2) mechanism_exposition:一句(非对白)里并列两个以上机制词,并且用定义句式
#      (生性/用来/若不在/必须……)把设定讲一遍。
# 只并列机制词而没有解释框架的句子是正常叙事(例如"他攥着腰牌转向铁索"),
# 机制词作为本场主题反复出现也不算复述——合同本身允许准确名称重复。
_LIMIT_NEAR_TERM_RE = re.compile(
    r"(?:只(?:能|够|会)|不(?:可|能)(?:恢复|再生|重复|超过|飞行|隔空|沾|湿|离手)?|"
    r"必须|否则|最多|至少|不得超过|一(?:次|炷香|刻|息)为限|以内)"
)
_DEFINITIONAL_RE = re.compile(
    r"(?:生性|习性|性极|灵性|规矩|铁律|条件|乃是|是一种|用于|用来|名为|叫作|称作|"
    r"若不在|不到|否则|必须|专门)"
)


def _detect_rule_recitation(text: str, case: Any) -> list[Finding]:
    findings: list[Finding] = []
    terms = [t for t in case.mechanism_terms if t]
    if not terms:
        return findings
    for span in split_sentences(text):
        if ("“" in span.text) or ('"' in span.text):
            continue  # 人物自己说出规则可以是有效写法,交给编辑判断
        hit_terms = [t for t in terms if t in span.text]
        if not hit_terms:
            continue
        explained = ""
        for term in hit_terms:
            pos = span.text.find(term)
            window = span.text[max(0, pos - 4): pos + len(term) + 8]
            if _LIMIT_NEAR_TERM_RE.search(window):
                explained = term
                break
        if explained:
            findings.append(
                Finding(
                    category="rule_recitation",
                    detector="mechanism_explained",
                    severity="soft",
                    quote=span.text.strip(),
                    offset=span.start,
                    detail=f"把条件与上限当成一句规则讲出来({explained})",
                )
            )
        elif len(hit_terms) >= 2 and _DEFINITIONAL_RE.search(span.text):
            findings.append(
                Finding(
                    category="rule_recitation",
                    detector="mechanism_exposition",
                    severity="soft",
                    quote=span.text.strip(),
                    offset=span.start,
                    detail=f"用定义句式把设定讲一遍:{'、'.join(hit_terms)}",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 4. 泛化强度比喻堆叠
# ---------------------------------------------------------------------------

_SIMILE_WORDS = ("仿佛", "好似", "宛如", "犹如", "恰似", "如同", "像是", "好像", "似的")
_SIMILE_STANDALONE = re.compile(r"(?<![如例比假譬诸宛犹恰好想让图影相不])(?:如|像)")
_SIMILE_FALSE_FRIENDS = (
    "如果", "如今", "如此", "例如", "比如", "譬如", "如何", "如下", "如实", "如意",
    "如期", "如数", "如常", "如故", "假设", "比方",
)
_INTENSITY_TERMS = (
    "咆哮", "怒吼", "撕心裂肺", "惊天动地", "震耳欲聋", "排山倒海", "铺天盖地",
    "如同巨手", "无形的手", "无处不在的", "如潮水", "如雷鸣", "野兽般", "巨手",
)
SIMILE_PARAGRAPH_LIMIT = 3
INTENSITY_PER_1000_LIMIT = 3


def _count_similes(paragraph: str) -> int:
    cleaned = paragraph
    for bad in _SIMILE_FALSE_FRIENDS:
        cleaned = cleaned.replace(bad, "□")
    count = sum(cleaned.count(w) for w in _SIMILE_WORDS)
    count += len(_SIMILE_STANDALONE.findall(cleaned))
    return count


def _detect_metaphor_stack(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for para in split_paragraphs(text):
        n = _count_similes(para.text)
        if n >= SIMILE_PARAGRAPH_LIMIT:
            findings.append(
                Finding(
                    category="metaphor_stack",
                    detector="paragraph_simile_density",
                    severity="soft",
                    quote=para.text.strip()[:160],
                    offset=para.start,
                    detail=f"同一段出现 {n} 处比喻标记(阈值 {SIMILE_PARAGRAPH_LIMIT})",
                )
            )
    hits = [(m.start(), term) for term in _INTENSITY_TERMS for m in re.finditer(re.escape(term), text)]
    limit = max(1, int(len(text) / 1000 * INTENSITY_PER_1000_LIMIT))
    if len(hits) > limit:
        hits.sort()
        offset, term = hits[0]
        findings.append(
            Finding(
                category="metaphor_stack",
                detector="intensity_term_density",
                severity="soft",
                quote=_sentence_at(text, offset),
                offset=offset,
                detail=f"泛化强度词 {len(hits)} 处,超过本长度上限 {limit}",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 5. 结果兑现 / 结尾
# ---------------------------------------------------------------------------

_MONEY_UNIT_RE = re.compile(r"(\d+|[一二两三四五六七八九十百千万零〇]+)(元|文|坛)")
_INTENT_ONLY = re.compile(r"(打算|准备|想着|以后再|改天|等以后|回头再|明天再)")


def _requirement_ok(text: str, paragraphs: list[Span], req: Any) -> tuple[bool, str]:
    if req.tokens_all and any(t not in text for t in req.tokens_all):
        missing = [t for t in req.tokens_all if t not in text]
        return False, f"缺少必要内容:{'、'.join(missing)}"
    patterns = getattr(req, "pattern_any", ())
    if req.tokens_any or patterns:
        hit = any(t in text for t in req.tokens_any)
        if not hit and patterns:
            hit = any(re.search(p, text) for p in patterns)
        if not hit:
            shown = '、'.join(req.tokens_any) or '、'.join(patterns)
            return False, f"未出现任一项:{shown}"
    if req.co_any:
        found = False
        for para in paragraphs:
            if (not req.tokens_any or any(t in para.text for t in req.tokens_any)) and any(
                c in para.text for c in req.co_any
            ):
                found = True
                break
        if not found:
            return False, f"未与任一项共现:{'、'.join(req.co_any)}"
    return True, "ok"
# ---------------------------------------------------------------------------
# 7. 未授权人物:亲属称谓与新名字
# ---------------------------------------------------------------------------
#
# 真实基线稿件里出现的两类问题,原先的禁用词表抓不到:
#   * real_stall-scene_writer 里母亲说「你哥......」——样本从未设定儿子,
#     这是凭空补出的家庭成员;
#   * real_ferry-scene_writer 里为解决钱的问题临时出现「老陈头」「张阿姨」。
#
# 判据:作者文字(case.brief)与人物表(case.cast)里出现过的关系词就是已授权的;
# 正文里出现其它关系词,说明稿件自己补了一个人。

# 未授权人物:把新的人写成主角亲属,或临时补出一个带称谓的具名人物。
#
# 真实基线里的两类问题:
#   * real_stall-scene_writer 母亲说「你哥......」——样本从未设定儿子;
#   * real_ferry-scene_writer 为解决钱的问题临时出现「老陈头」「张阿姨」。
#
# 第一版实现用"关系词是否出现在作者文字里"做判据,误报很多(「姑娘」里的「娘」、
# 「老孙家的媳妇」里的「媳妇」)。现在只认两类可复核的证据:
#   1) 带人称前缀的亲属称谓(你哥 / 她的妈妈),且该亲属关系作者从未设定;
#   2) 带称谓后缀的新具名人物(张阿姨 / 老陈头 / 孙家汉子),且作者文字与人物表里
#      都没有这个人。
_RELATION_FAMILIES: dict[str, tuple[str, ...]] = {
    "mother": ("妈妈", "母亲", "妈", "娘"),
    "father": ("爸爸", "父亲", "爸", "爹"),
    "brother": ("哥哥", "大哥", "二哥", "弟弟", "兄弟", "哥", "弟"),
    "sister": ("姐姐", "大姐", "妹妹", "姐", "妹"),
    "child": ("儿子", "女儿", "孩子"),
    "spouse": ("丈夫", "老婆", "妻子", "媳妇", "爱人"),
    "uncle_aunt": ("叔叔", "伯伯", "舅舅", "姑姑", "阿姨", "婶"),
    "grandparent": ("爷爷", "奶奶", "外公", "外婆", "姥姥", "姥爷"),
}
_GENERIC_FAMILY_WORDS = ("家人", "家属", "亲属", "亲戚")
_GENERIC_FAMILIES = ("spouse", "child")
_POSSESSIVE_RELATION_RE = re.compile(
    r"(?:你|您|我|他|她|俺|人家)(?:的)?(哥哥|大哥|二哥|弟弟|姐姐|大姐|妹妹|"
    r"妈妈|母亲|爸爸|父亲|儿子|女儿|孩子|丈夫|老婆|妻子|媳妇|叔叔|伯伯|舅舅|"
    r"姑姑|阿姨|爷爷|奶奶|外公|外婆|姥姥|姥爷|哥|姐|弟|妹|妈|爸|娘|爹|婶)"
)
_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章"
    "云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常"
    "乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞"
    "熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁"
)
_TITLE_SUFFIXES = (
    "大叔", "大婶", "大爷", "大娘", "大哥", "大姐", "阿姨", "叔叔", "伯伯", "婶子",
    "汉子", "小哥", "叔", "姨", "婶", "哥", "姐", "伯", "头",
)
# 「石头」「老头」「毛头纸」这类普通名词不是人物;命中这些一律不算新人物。
_GENERIC_TITLE_NOUNS = (
    "石头", "木头", "骨头", "馒头", "块头", "前头", "后头", "上头", "下头", "里头",
    "外头", "一头", "低头", "抬头", "回头", "念头", "关头", "零头", "苗头", "户头",
    "舌头", "指头", "拳头", "枕头", "浪头", "源头", "风头", "尽头", "口头", "老头",
    "毛头", "手头", "地头", "山头", "码头", "街头", "村头", "炕头", "肩头", "膝头",
)
_NEW_TITLE_RE = re.compile(
    r"(?:^|(?<=[\s\n。!?、,,“”「」‘’:：])|(?<=的)|(?<=个)|(?<=见))"
    r"([\u4e00-\u9fa5]{1,2}(?:" + "|".join(_TITLE_SUFFIXES) + r"))"
)
_NAME_PREFIXES = ("老", "小", "阿", "大")


def _authorized_relations(case: Any) -> tuple[set[str], set[str]]:
    """返回作者文字已授权的亲属关系族与已授权人名。"""
    brief = case.brief or ""
    families: set[str] = set()
    for family, words in _RELATION_FAMILIES.items():
        if any(word in brief for word in words):
            families.add(family)
    if any(word in brief for word in _GENERIC_FAMILY_WORDS):
        families.update(_GENERIC_FAMILIES)
    return families, set(case.cast)


def _family_of(relation: str) -> str:
    for family, words in _RELATION_FAMILIES.items():
        if relation in words:
            return family
    return ""


def _detect_unauthorized_persons(text: str, case: Any) -> list[Finding]:
    findings: list[Finding] = []
    families, known_names = _authorized_relations(case)
    brief = case.brief or ""
    for m in _POSSESSIVE_RELATION_RE.finditer(text):
        relation = m.group(1)
        family = _family_of(relation)
        if not family or family in families:
            continue
        findings.append(
            Finding(
                category="fact_violation",
                detector="invented_relation",
                severity="hard",
                quote=_sentence_at(text, m.start()),
                offset=m.start(),
                detail=f"正文把「{m.group(0)}」写成主角亲属,但作者文字未设定该关系({family})",
            )
        )
    for m in _NEW_TITLE_RE.finditer(text):
        name = m.group(1)
        if name in known_names or any(known in name for known in known_names) or name in brief:
            continue
        if name in _GENERIC_TITLE_NOUNS:
            continue
        if name[0] not in _SURNAMES and not name.startswith(_NAME_PREFIXES):
            continue
        findings.append(
            Finding(
                category="fact_violation",
                detector="unauthorized_new_character",
                severity="soft",
                quote=_sentence_at(text, m.start()),
                offset=m.start(),
                detail=f"出现作者文字未设定的具名人物「{name}」,需核对是否为新补的人物",
            )
        )
    return findings


def _detect_outcomes(text: str, case: Any) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    paragraphs = split_paragraphs(text)
    checks: list[dict[str, Any]] = []
    for req in case.required_outcomes:
        ok, detail = _requirement_ok(text, paragraphs, req)
        checks.append({"id": req.id, "desc": req.desc, "ok": ok, "detail": detail})
        if not ok:
            findings.append(
                Finding(
                    category="outcome_unfulfilled",
                    detector=f"outcome:{req.id}",
                    severity="hard",
                    quote="",
                    offset=-1,
                    detail=f"本场要求未兑现({req.desc}):{detail}",
                )
            )
    if case.ending_mode == "settled":
        tail_start = max(0, len(text) - max(220, int(len(text) * 0.25)))
        tail = text[tail_start:]
        if case.ending_required_any and not any(t in tail for t in case.ending_required_any):
            findings.append(
                Finding(
                    category="outcome_unfulfilled",
                    detector="ending_anchor_missing",
                    severity="hard",
                    quote=tail.strip()[-160:],
                    offset=tail_start,
                    detail=f"结尾未落到要求的状态:{'、'.join(case.ending_required_any)}",
                )
            )
        sentences = split_sentences(text)
        last_two = "".join(s.text for s in sentences[-2:])
        if _INTENT_ONLY.search(last_two) and not re.search(r"[了过]", last_two):
            findings.append(
                Finding(
                    category="outcome_unfulfilled",
                    detector="ending_intent_only",
                    severity="hard",
                    quote=last_two.strip(),
                    offset=sentences[-2].start if len(sentences) > 1 else 0,
                    detail="结尾只有打算,没有已经发生的行动",
                )
            )
    return findings, checks


# ---------------------------------------------------------------------------
# 6. 事实改动 / 禁止元素
# ---------------------------------------------------------------------------

def _normalize_number(token: str) -> str:
    return token.strip()


def _detect_facts(text: str, case: Any) -> tuple[list[Finding], dict[str, list[dict[str, Any]]]]:
    findings: list[Finding] = []
    paragraphs = split_paragraphs(text)
    keep_checks: list[dict[str, Any]] = []
    forbid_checks: list[dict[str, Any]] = []

    for req in case.must_keep:
        ok, detail = _requirement_ok(text, paragraphs, req)
        keep_checks.append({"id": req.id, "desc": req.desc, "ok": ok, "detail": detail})
        if not ok:
            findings.append(
                Finding(
                    category="fact_violation",
                    detector=f"must_keep:{req.id}",
                    severity="hard",
                    quote="",
                    offset=-1,
                    detail=f"必须保持的事实未成立({req.desc}):{detail}",
                )
            )

    for req in case.forbidden:
        hit_offsets = [(text.find(t), t) for t in req.tokens_any if t in text]
        hit_offsets = [(o, t) for o, t in hit_offsets if o >= 0]
        forbid_checks.append(
            {
                "id": req.id,
                "desc": req.desc,
                "ok": not hit_offsets,
                "detail": "ok" if not hit_offsets else f"出现禁止项:{'、'.join(t for _, t in hit_offsets)}",
            }
        )
        for offset, token in sorted(hit_offsets):
            findings.append(
                Finding(
                    category="fact_violation",
                    detector=f"forbidden:{req.id}",
                    severity="hard",
                    quote=_sentence_at(text, offset),
                    offset=offset,
                    detail=f"新增了明令禁止的元素({token}):{req.desc}",
                )
            )

    allowed = set()
    for token in case.numbers_allowed:
        allowed.add(token)
        m = _MONEY_UNIT_RE.fullmatch(token)
        if m:
            allowed.add(m.group(1))
    for m in _MONEY_UNIT_RE.finditer(text):
        value = _normalize_number(m.group(1))
        if value in allowed:
            continue
        findings.append(
            Finding(
                category="fact_violation",
                detector="unexpected_quantity",
                severity="soft",
                quote=_sentence_at(text, m.start()),
                offset=m.start(),
                detail=f"出现样本未给定的数量 {m.group(0)!r},需核对是否改动既定数字",
            )
        )
    return findings, {"must_keep": keep_checks, "forbidden": forbid_checks}


# ---------------------------------------------------------------------------
# 语言指标(不构成 finding,用于前后对照)
# ---------------------------------------------------------------------------

_AI_PHRASE_FALLBACK = (
    "不禁", "顿时", "瞬间", "仿佛", "空气仿佛", "不由得", "心中五味杂陈",
    "百感交集", "值得一提的是", "不得不说", "与此同时", "深吸一口气", "缓缓",
    "微微一怔", "嘴角勾起", "眼底闪过", "若有所思",
)


def _ai_phrase_hits(text: str) -> dict[str, int]:
    phrases: list[str] = list(_AI_PHRASE_FALLBACK)
    blacklist: dict[str, list[str]] = {}
    try:  # 复用生产黑名单,保证指标与线上规则同源
        from app.services.prompts.anti_ai_rules_zh import AI_PHRASE_BLACKLIST

        if isinstance(AI_PHRASE_BLACKLIST, dict):
            blacklist = AI_PHRASE_BLACKLIST
            phrases = [w for words in blacklist.values() for w in words]
    except Exception:  # 黑名单不可用时退回内置子集,不静默变成零命中
        pass
    hits: dict[str, int] = {}
    for phrase in phrases:
        if not isinstance(phrase, str) or len(phrase) < 2:
            continue
        n = text.count(phrase)
        if n:
            hits[phrase] = n
    return dict(sorted(hits.items()))


def _metrics(text: str) -> dict[str, Any]:
    chars = len(text)
    sentences = split_sentences(text)
    paragraphs = split_paragraphs(text)
    lengths = [len(s.text.strip()) for s in sentences] or [0]
    dialogue_chars = sum(len(m.group(0)) for m in re.finditer(r"[“\"][^”\"]*[”\"]", text))
    ai_hits = _ai_phrase_hits(text)
    return {
        "chars": chars,
        "paragraphs": len(paragraphs),
        "sentences": len(sentences),
        "avg_sentence_len": round(sum(lengths) / len(lengths), 2),
        "max_sentence_len": max(lengths),
        "min_sentence_len": min(lengths),
        "dialogue_ratio": round(dialogue_chars / chars, 3) if chars else 0.0,
        "de_per_1000": round(text.count("的") / chars * 1000, 2) if chars else 0.0,
        "simile_per_1000": round(_count_similes(text) / chars * 1000, 2) if chars else 0.0,
        "ai_phrase_total": sum(ai_hits.values()),
        "ai_phrases": ai_hits,
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def detect(case: Any, text: str) -> Detection:
    """对一份正文跑完六类检测;返回可序列化、可重复的结果。"""
    body = text or ""
    findings: list[Finding] = []
    findings.extend(_detect_pov_breach(body, case))
    findings.extend(_detect_instruction_leak(body))
    findings.extend(_detect_rule_recitation(body, case))
    findings.extend(_detect_metaphor_stack(body))
    outcome_findings, outcome_checks = _detect_outcomes(body, case)
    findings.extend(outcome_findings)
    fact_findings, fact_checks = _detect_facts(body, case)
    findings.extend(fact_findings)
    findings.extend(_detect_unauthorized_persons(body, case))
    return Detection(
        case_id=case.id,
        chars=len(body),
        findings=findings,
        metrics=_metrics(body),
        requirements={
            "required_outcomes": outcome_checks,
            "must_keep": fact_checks["must_keep"],
            "forbidden": fact_checks["forbidden"],
        },
    )