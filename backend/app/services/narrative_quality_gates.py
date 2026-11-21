"""Genre-agnostic narrative generation principles.

This module turns evaluator issue tags into reusable, cross-project writing
principles.  These rules are meant to be internalized before generation and
used for diagnostics after scoring. They should not be used as a post-output
blocking gate in the happy path.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from app.services.prose_quality_rules import render_prose_quality_prompt

VIOLATION_TAG_RE = re.compile(r"\[([a-z_]+_violation)\]")

# Single source of truth for the chapter-blueprint contract version. Every
# version reference in this module's rendered prompts must use this constant.
BLUEPRINT_VERSION = "v4.14"

@dataclass(frozen=True)
class QualityGateRule:
    tag: str
    title: str
    root_cause: str
    detection: str
    repair_action: str
    hard_gate: str  # pre-generation principle; not a post-output blocker

ALLOWED_REPAIR_ACTIONS: tuple[str, ...] = (
    "补支撑",
    "对齐作者要求与证据",
    "纠正冲突事实",
    "核对人物与资源来源",
    "保留未涉及的情节",
)

QUALITY_GATE_RULES: dict[str, QualityGateRule] = {
    "time_rule_violation": QualityGateRule(
        tag="time_rule_violation", title="时间与顺序",
        root_cause="明确时间与行动冲突。",
        detection="核对原文时刻、前后顺序与实际可用时间。",
        repair_action="修正冲突时间或补必要连接，不强行打断。",
        hard_gate="动作与本书已写时间相容，不设统一动作数量。",
    ),
    "space_rule_violation": QualityGateRule(
        tag="space_rule_violation", title="位置与路径",
        root_cause="已写位置或阻隔使动作无法成立。",
        detection="对照动作前后位置、递物路径和中间变化；正常省略不算瞬移。",
        repair_action="修补影响结果的路径或阻隔，保留可理解的省略。",
        hard_gate="关键动作有可行路径，不要求每步移动都写明。",
    ),
    "information_rule_violation": QualityGateRule(
        tag="information_rule_violation", title="信息与判断",
        root_cause="人物无来源知情或结论超出证据。",
        detection="区分正文事实、角色猜测、读者信息与人物已知。",
        repair_action="纠正无来源判断；已有依据的确认不降级。",
        hard_gate="信息与人物已知范围相容；有充分证据可以确认。",
    ),
    "mechanism_rule_violation": QualityGateRule(
        tag="mechanism_rule_violation", title="机制边界",
        root_cause="动作违反本书已经建立的规则。",
        detection="核对相关机制的已设条件与本次动作。",
        repair_action="修正具体冲突，不给普通工具强加冷却和副作用。",
        hard_gate="遵守已设条件，不凭通用模板新增规则。",
    ),
    "power_resource_violation": QualityGateRule(
        tag="power_resource_violation", title="权限与资源",
        root_cause="动作调用了未获得的关键权限或资源。",
        detection="核对本书真实权限、资源来源和合作动机。",
        repair_action="补必要来源或纠正调用，不强加损失和敌意。",
        hard_gate="顺利合作与无损成功可以成立，不能无故获得关键资源。",
    ),
    "result_strength_violation": QualityGateRule(
        tag="result_strength_violation", title="结果与作者要求",
        root_cause="结果与作者要求或已有证据不相符。",
        detection="比较本章明确结果、实际行动与证据强度。",
        repair_action="按作者要求和事实修正结果，不能一律延后或降级。",
        hard_gate="应确认的结果可以确认，应完成的事件要完成。",
    ),
    "expression_contract_violation": QualityGateRule(
        tag="expression_contract_violation", title="人物表达与阅读",
        root_cause="表达造成具体理解障碍或违背作者指定的语言要求。",
        detection="结合人物关系和语境定位问题，不以词频和比例定错。",
        repair_action="按原文问题作最小调整，保留有作用的表达。",
        hard_gate="不强制问答、短句、停顿和心理描写数量。",
    ),
    "mundane_plausibility_violation": QualityGateRule(
        tag="mundane_plausibility_violation", title="日常常识与章内自洽",
        root_cause="人物或物件状态与已写事实冲突。",
        detection="核对同一对象的属性、数量、持有人、位置和中间变化。",
        repair_action="纠正冲突事实；不能以未指定惯例替作者补前提。",
        hard_gate="原文事实前后一致，限知视角不进入他人内心或身体感觉。",
    ),
}

BLOCKING_CONTRACT_VIOLATION_TYPES: frozenset[str] = frozenset(QUALITY_GATE_RULES)

REUSABLE_REVISE_BUCKETS: tuple[tuple[str, tuple[str, ...], str], ...] = tuple(
    (rule.title, (rule.tag,), rule.repair_action) for rule in QUALITY_GATE_RULES.values()
)


CHINESE_PROSE_MECHANICS_PROMPT = """\
【中文小说行文与事实自检】
按作者指定的题材、叙述视角和情节要求写作。人物怎么开口、为什么沉默、怎样行动，应符合当下处境及彼此关系。
停顿、心理描写、回忆、长对白、连续短句和重复词语都可以有叙事作用；不按词频清零，不强制对白比例、句长、微动作或比喻配额，不为凑字数补情节。
动作应与已经写出的地点、时间、人物状态和物件去向衔接。影响结果的阻隔与交接要交代清楚；读者能理解的日常移动可以省略，不把每一步都写成操作说明。
限知视角不直接断言其他人的内心和身体感觉；可观察的外部动作无需每句加“看见”。全知或多视角按作者约定执行。
本章明确要求完成的事件要落实，承诺、尝试和已完成不能混为一谈；不要为了制造悬念把应完成的事件统一降为疑点。
重复描写要结合语境判断：同一动作在新处境、不同回应或递进意义中可以准确重复，不为避重换成失真的近义词。
检查具体事实冲突时须同时核对前后原句及中间变化；词表命中、比例偏高或未交代无关细节不等于矛盾。
只输出正文，不附写作分析、章节编号或结束标签；人物正常谈论书籍章节的内容不在此限。
"""

def issue_violation_type(issue: dict[str, Any]) -> str:
    for key in ("violation_type", "type", "tag"):
        raw = issue.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    text = " ".join(str(issue.get(key) or "") for key in ("description", "suggestion"))
    match = VIOLATION_TAG_RE.search(text)
    return match.group(1) if match else ""

def coerce_issues(eval_obj: Any) -> list[dict[str, Any]]:
    if eval_obj is None:
        return []
    if isinstance(eval_obj, dict):
        raw = eval_obj.get("issues") or []
    else:
        raw = getattr(eval_obj, "issues", None) or []
    return [x for x in raw if isinstance(x, dict)]

def group_issues_by_violation_type(issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        vtype = issue_violation_type(issue) or "untyped_issue"
        grouped.setdefault(vtype, []).append(issue)
    return grouped

def blocking_contract_violations(eval_obj: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for issue in coerce_issues(eval_obj):
        vtype = issue_violation_type(issue)
        if vtype in BLOCKING_CONTRACT_VIOLATION_TYPES and vtype not in seen:
            found.append(vtype)
            seen.add(vtype)
    return found

def blocking_contract_violation_set(eval_obj: Any) -> set[str]:
    return set(blocking_contract_violations(eval_obj))

def repair_method_for(vtype: str) -> str:
    rule = QUALITY_GATE_RULES.get(vtype)
    if rule:
        return f"根因：{rule.root_cause} 修复：{rule.repair_action} 硬门槛：{rule.hard_gate}"
    return "先核对诊断与原文、作者要求是否一致，再作必要调整；不随机改稿或改变结果。"

def reusable_bucket_lines(issues: list[dict[str, Any]]) -> list[str]:
    grouped = group_issues_by_violation_type(issues)
    lines: list[str] = []
    for title, vtypes, instruction in REUSABLE_REVISE_BUCKETS:
        count = sum(len(grouped.get(vtype, [])) for vtype in vtypes)
        if count > 0:
            lines.append(f"- {title}（{count}）：{instruction}")
    return lines

def contract_hard_gate_prompt() -> str:
    lines = ["【生成前内化约束】先以作者本章要求和本书已建立的事实为准，以下检查只用于发现具体冲突："]
    for rule in QUALITY_GATE_RULES.values():
        lines.append(f"- {rule.tag}｜{rule.title}：{rule.hard_gate} 检测：{rule.detection}")
    lines.append("若内部自检发现风险，优先用以下写作决策处理：" + " / ".join(ALLOWED_REPAIR_ACTIONS) + "；目标是第一稿直接满足要求，评分后仅作诊断。")
    lines.append(CHINESE_PROSE_MECHANICS_PROMPT)
    lines.append(render_prose_quality_prompt())
    return "\n".join(lines)

GOLDEN_OPENING_RULES = """\
【开卷叙事关注（前三章适用）】
帮助读者尽早理解人物是谁、此刻在做什么、为什么值得继续读。进入事件的速度服从题材和作者安排，不固定前三百字必须发生危机，不禁止必要的天气、回忆和背景。
过渡和静场也要有明确的叙事作用；章节结尾按本章要求完成，不强制制造钩子或新危险。
"""


def preflight_scene_blueprint_prompt(chapter_idx: int | None = None) -> str:
    """Prepare author-defined outcomes and continuity without imposing plot quotas."""
    chapter_label = f"[CH-{chapter_idx}]" if chapter_idx else "本章"
    prompt = f"""
【direct_generation_first_{BLUEPRINT_VERSION}｜本章事件与连续性】
为{chapter_label}写正文前，核对作者要求、大纲和已发生事实。准备工作留在内部，不输出规划台账、分析、JSON或自检说明。

一、author_outcomes（本章应交付什么）
逐项辨认作者本章要求完成、确认、保留或延后的事件；区分意图、尝试、承诺与实际完成。大纲没有逐笔写出的连接动作、人物反应和环境细节可以自然补写，但不能新增改变主线的关键资源、规则或人物关系。
应完成的事件要写出结果；未来才到期的事件不抢写。结尾可明确确认、自然收束或留下悬念，不能一律降为疑点。

二、continuity_state（从什么事实接着写）
从前文真实正文承接时间、位置、人物状态、物件数量及持有人、关键消息和权限。计划中的结果不算已发生，摘要与正文冲突时不能用摘要覆盖正文。
对反复出现的人物或物件保持身份和状态连续；每次变化核对谁做了什么、变化后在哪里。已完成的交接、离场或得知消息不要无故重演。

三、causal_connections（哪些动作需要交代）
动作须在已写明的时间、位置、阻隔和权限下能够完成。关键递物、救援、转移与突破要交代必要连接，不靠凭空出现的工具或权限。
日常出门、上车、走到另一处等，读者能理解时可省略中间步骤。帮助者可以出于普通同事情谊或既有关系配合；不强制每次成功都付损失，不为每把钥匙编一套机关。
需要人或资源推进时,只用作者要求和已写正文里出现过的人和东西;不要为解决问题补出亲属、帮手、债主、雇主或一笔新的钱,也不要临时多出一件可用之物或一项本事。局面一时解决不了,就让它保持未解决。
紧迫程度决定可用时间与对白展开量，不用固定动作数或问答数代替判断。信息来源决定判断强度：充分证据可以确认，未获得的信息不能直接知道。

四、narrative_purpose（如何写成小说）
把准备工作写成人物的行动、对白、感受与取舍，不把正文写成办事说明或核验清单。详写影响处境和关系的关键片刻，略写重复流程；不为了给每段添加状态变化而制造新冲突。
风格和节奏服从作者选择。静场、回忆、长对白、直接表达意图与准确重复都有可能有效，不用通用禁令代替中文阅读判断。

五、completion_check（收尾核对）
读完整章后检查：作者本章到期的事件是否真的发生；人物状态和物件去向是否前后一致；是否进入了指定视角以外的内心或触觉；是否只写了过程而漏掉结果。修补具体遗漏，不为凑字数堆环境、感慨或制度解释。

六、chinese_prose_mechanics（行文观察，不是文学判决）
{CHINESE_PROSE_MECHANICS_PROMPT.strip()}
{render_prose_quality_prompt()}
""".strip()
    if chapter_idx is not None and 1 <= chapter_idx <= 3:
        prompt += "\n\n" + GOLDEN_OPENING_RULES.strip()
    return prompt
