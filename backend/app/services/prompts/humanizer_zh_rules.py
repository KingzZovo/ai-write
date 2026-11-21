"""Humanizer-zh 去AI味规则（结构性 AI 痕迹）。

Adapted from op7418/Humanizer-zh —— 基于 Wikipedia「Signs of AI writing」
（WikiProject AI Cleanup），blader/humanizer 的中文化版本。

本模块只收 Humanizer-zh 中**结构性**的 AI 痕迹，与既有词表正交互补：

- ``prompts/anti_ai_rules_zh``（QMAI）管「网文腔」：陈词滥调、机械小动作、情绪直陈；
- ``checkers/anti_ai_checker`` 管「密度统计」：AI 词密度、的字、四字成语、句式单调；
- 本模块管「论述腔/翻译腔的结构套路」：否定式排比、三段式法则、同义词循环、
  虚假范围、系动词回避、句尾强行升华、过度限定。

这些套路在 QMAI/checker 的词表与密度里都查不到——它们是**句法骨架**层面的
AI 指纹，单看每个词都正常，组合成固定结构才露馅。

为何对本项目重要：神裔 ch1 审查暴露的「同一画面被换说法重述第二遍」
（草稿叠写痕迹）正是 ``synonym_cycling`` 规则要压住的东西；句尾「象征着…
标志着…」式空洞升华是 AI 叙述最常见的拔高腔。

消费方：
- ``prose_quality_rules.render_prose_quality_prompt()`` 末尾追加
  ``render_humanizer_prompt_block()``（单一注入点，同时进生成与润色 prompt）；
- ``checkers/anti_ai_checker.AntiAIChecker`` 调用 ``scan_humanizer_structural()``
  产出诊断 issue（仅 low/medium，不做硬门，避免过度返工）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class HumanizerRule:
    """一条结构性去AI味规则。

    ``patterns`` 为空时表示该规则只做 prompt 侧引导、不做确定性检测
    （如同义词循环、三段式——无法在没有实体知识/语义的前提下可靠正则化）。
    """

    rule_id: str
    title: str
    problem: str
    bad_examples: tuple[str, ...]
    good_examples: tuple[str, ...]
    prompt_instruction: str
    patterns: tuple[str, ...] = ()
    # 命中 >= warn_hits 记 low；>= high_hits 记 medium。
    warn_hits: int = 2
    high_hits: int = 3
    suggestion: str = ""


HUMANIZER_RULES: tuple[HumanizerRule, ...] = (
    HumanizerRule(
        rule_id="negative_parallelism",
        title="否定式排比",
        problem="「不仅仅是X，而是Y」「不是X，而是Y」「与其说…不如说…」这类对仗式拔高被过度使用，制造虚假的深刻。",
        bad_examples=(
            "这不是结束，而是另一个开始。",
            "他要的不仅仅是活下去，更是活成别人不敢想的样子。",
            "与其说他在逃跑，不如说他在重新选择战场。",
        ),
        good_examples=(
            "他知道这事还没完。",
            "他不只想活下去。",
            "他在找退路。",
        ),
        prompt_instruction=(
            "negative_parallelism：禁用「不仅仅是X，而是Y」「不是X，而是Y」"
            "「与其说…不如说…」这类对仗式拔高；直接把意思说出来。"
        ),
        patterns=(
            r"不仅仅?是[^。！？\n]{1,40}(?:而是|更是|还是|而且)",
            r"不是[^。！？，\n]{1,30}，\s*而是",
            r"与其说[^。！？\n]{1,30}，?\s*不如说",
        ),
        suggestion="删掉否定式排比，直接陈述事实或心理，不要用对仗结构强行拔高。",
    ),
    HumanizerRule(
        rule_id="shallow_significance",
        title="句尾强行升华",
        problem="叙述句以「象征着…/标志着…/反映了…/彰显了…/预示着…/意味着…」收尾，给画面贴一层空洞的意义标签。",
        bad_examples=(
            "那道银光在他臂上游走，象征着某种古老力量的苏醒。",
            "断裂的门轴掉在地上，预示着这栋楼再没有退路。",
            "他攥紧了工牌，彰显出一种不肯认输的执拗。",
        ),
        good_examples=(
            "银光在他臂上游走，皮肤底下浮起一道纹路。",
            "门轴断了，掉在地上。",
            "他把工牌攥得更紧。",
        ),
        prompt_instruction=(
            "shallow_significance：叙述句不要以「象征着/标志着/反映了/彰显了/"
            "预示着/意味着/见证了」收尾强行升华；写画面本身，意义交给读者。"
        ),
        patterns=(
            r"(?:象征着|标志着|预示着|意味着|见证[了着]|折射出|映照出|诠释着)[^。！？\n]{0,30}[。！？]",
            r"彰显[了着出][^。！？\n]{0,30}[。！？]",
            r"反映[了出][^。！？\n]{0,30}[。！？]",
        ),
        suggestion="把句尾的意义标签删掉，只留具体动作或画面；让读者自己体会分量。",
    ),
    HumanizerRule(
        rule_id="false_range",
        title="虚假范围",
        problem="「从X到Y，从A到B」式排比扫描，X/Y 并不在同一尺度上，只为显得包罗万象。",
        bad_examples=(
            "从街角的早餐摊到城市尽头的写字楼，从清晨的露水到深夜的霓虹，这座城什么都有。",
        ),
        good_examples=(
            "这条街上有早餐摊、五金店和一家通宵的便利店。",
        ),
        prompt_instruction=(
            "false_range：禁用「从X到Y，从A到B」式空泛排比扫描；"
            "要列举就给三两个具体、同尺度的东西。"
        ),
        patterns=(
            r"从[^，。！？\n]{1,15}到[^，。！？\n]{1,15}[，、][^。！？\n]{0,10}从[^，。！？\n]{1,15}到",
        ),
        warn_hits=1,
        high_hits=2,
        suggestion="把虚假范围改成两三个具体、同一尺度的实例。",
    ),
    HumanizerRule(
        rule_id="copula_avoidance",
        title="系动词回避",
        problem="能用「是/有」的地方硬写成「作为…的存在」「充当…」「堪称…」，把简单判断绕复杂。",
        bad_examples=(
            "他作为这条街上唯一的外来者的存在，格外扎眼。",
            "那把锈刀堪称他全部的家当。",
        ),
        good_examples=(
            "他是这条街上唯一的外来者，格外扎眼。",
            "那把锈刀是他全部的家当。",
        ),
        prompt_instruction=(
            "copula_avoidance：能用「是/有」就别写「作为…的存在」「充当…」"
            "「堪称…」；简单判断用简单系动词。"
        ),
        patterns=(
            r"作为[^。！？\n]{1,24}的(?:存在|象征|化身|代名词|缩影)",
            r"堪称[^。！？\n]{1,15}",
        ),
        suggestion="把「作为…的存在/充当/堪称」还原成「是」或「有」。",
    ),
    HumanizerRule(
        rule_id="over_qualification",
        title="过度限定",
        problem="叠加「可能/也许/大概/似乎/恐怕」等模糊限定词，把一句话软到没有重量。",
        bad_examples=(
            "他似乎大概觉得，这里也许可能不太安全。",
        ),
        good_examples=(
            "他觉得这里不安全。",
        ),
        prompt_instruction=(
            "over_qualification：删掉叠加的「可能/也许/大概/似乎/恐怕」，"
            "一个限定词就够，多数时候一个都不要。"
        ),
        patterns=(
            r"(?:可能|也许|或许|大概|似乎|恐怕|约莫|多半)[^。！？\n]{0,6}"
            r"(?:可能|也许|或许|大概|似乎|恐怕|约莫|多半)",
        ),
        warn_hits=1,
        high_hits=3,
        suggestion="一句话里只留一个限定词，能删则删。",
    ),
    # 以下两条无法可靠正则化，只做 prompt 侧引导（patterns 为空）。
    HumanizerRule(
        rule_id="synonym_cycling",
        title="同义词循环 / 同画面重述",
        problem=(
            "AI 的重复惩罚导致同一人物/对象被换称呼轮流指代（主角→少年→那道身影→"
            "全名），或同一画面被换说法重述第二遍——后者正是草稿叠写残留。"
        ),
        bad_examples=(
            "林照推开门。少年看见骨架。那道身影僵在原地。他向后退。",
            "他看见花衬衫骨架转过头……（下滑时）那个穿花格衬衫的头骨已经转过大半个弧度。",
        ),
        good_examples=(
            "林照推开门，看见骨架，向后退。",
            "同一画面只写一次，下滑时只补一个新细节（蓝拖鞋），不重述走廊全景。",
        ),
        prompt_instruction=(
            "synonym_cycling：同一人物/对象在相邻段落只用一个稳定称呼，"
            "不要为避免重复轮换「主角/少年/那道身影/全名」；同一画面只写一次，"
            "二次出现只补一个新信息，禁止换说法把上文景物重述一遍（草稿叠写痕迹）。"
        ),
        suggestion="统一称呼；删掉对同一画面的二次重述，只保留增量信息。",
    ),
    HumanizerRule(
        rule_id="rule_of_three",
        title="三段式法则",
        problem="把意思硬凑成三项并列（A、B、C）显得全面，是 AI 最爱的节奏。",
        bad_examples=(
            "他需要的是勇气、智慧和一点点运气。",
            "这里有破败、有沉默、有挥之不去的腐味。",
        ),
        good_examples=(
            "他需要运气。",
            "这里又破又静，一股腐味散不掉。",
        ),
        prompt_instruction=(
            "rule_of_three：不要把意思硬凑成三项并列显得全面；"
            "两项或四项更像人写的，单项最有力。"
        ),
        suggestion="把三项并列拆成两项或合成一句，别凑整。",
    ),
)


# 预编译可检测规则的 pattern。
_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    rule.rule_id: tuple(re.compile(p) for p in rule.patterns)
    for rule in HUMANIZER_RULES
    if rule.patterns
}

_RULE_BY_ID: dict[str, HumanizerRule] = {rule.rule_id: rule for rule in HUMANIZER_RULES}


def rule_by_id(rule_id: str) -> HumanizerRule:
    return _RULE_BY_ID[rule_id]


def scan_humanizer_structural(text: str) -> list[dict[str, object]]:
    """扫描文本中的结构性 AI 痕迹。

    返回每条**命中阈值**的规则一个 dict：
    ``{rule_id, title, severity, hits, examples, suggestion}``。
    severity 仅 ``low``/``medium``——本扫描是诊断，不做硬门，避免过度返工。
    清洁文本返回空列表。
    """
    findings: list[dict[str, object]] = []
    if not text:
        return findings

    for rule_id, compiled in _COMPILED.items():
        rule = _RULE_BY_ID[rule_id]
        matched: list[str] = []
        for pat in compiled:
            for m in pat.finditer(text):
                snippet = m.group(0).strip()
                if snippet:
                    matched.append(snippet)
        hits = len(matched)
        if hits < rule.warn_hits:
            continue
        severity = "medium" if hits >= rule.high_hits else "low"
        # 去重并截断示例，避免 issue 描述过长。
        seen: list[str] = []
        for snippet in matched:
            if snippet not in seen:
                seen.append(snippet)
            if len(seen) >= 3:
                break
        findings.append(
            {
                "rule_id": rule_id,
                "title": rule.title,
                "severity": severity,
                "hits": hits,
                "examples": tuple(seen),
                "suggestion": rule.suggestion,
            }
        )
    return findings


def render_humanizer_prompt_block(max_chars: int = 1400) -> str:
    """渲染注入生成/润色 prompt 的紧凑结构性规则块。

    预算内按行装入，超预算的行跳过。行序即优先级——
    先排能确定性检测、危害最大的结构套路（否定式排比、句尾升华、同义词循环），
    再排其余。与 ``render_anti_ai_prompt_block`` 同构。
    """
    # 优先级排序：检测器能逮到的 + 神裔实证最痛的排前。
    order = (
        "negative_parallelism",
        "shallow_significance",
        "synonym_cycling",
        "rule_of_three",
        "copula_avoidance",
        "over_qualification",
        "false_range",
    )

    lines: list[str] = [
        "【humanizer_zh｜去AI味·结构性痕迹（源自 Wikipedia「Signs of AI writing」）】",
        "以下结构套路单看每个词都正常，组合成固定骨架就露 AI 马脚；正文与润色一律规避：",
    ]
    for rule_id in order:
        rule = _RULE_BY_ID[rule_id]
        lines.append(f"- {rule.rule_id}｜{rule.title}：{rule.prompt_instruction}")
        if rule.bad_examples and rule.good_examples:
            lines.append(
                "  坏例：" + rule.bad_examples[0] + "　好例：" + rule.good_examples[0]
            )

    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)  # +1 for the joining newline
        if used + cost > max_chars:
            continue
        out.append(line)
        used += cost
    return "\n".join(out)
