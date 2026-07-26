"""Chapter-level quality gate for mechanical prose traces.

This service re-evaluates generated chapter text after the main generator
finishes, optionally performs a targeted rewrite pass, and returns the best
available text together with safe diagnostics for API events / metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import os
import re
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chinese_prose_mechanics_checker import (
    ChineseProseMechanicsReport,
    analyze_chinese_prose_mechanics,
)
from app.services.narrative_quality_gates import contract_hard_gate_prompt
from app.services.prompt_registry import run_text_prompt

logger = logging.getLogger(__name__)

QualityGateStatus = Literal[
    "passed",
    "skipped",
    "improved_warning",
    "rewrite_failed",
    "blocked",
]

_MIN_LENGTH_RATIO = 0.70
_MIN_TARGET_WORD_RATIO = 0.50
_DEFAULT_MAX_REWRITE_ROUNDS = int(os.getenv("CHAPTER_MAX_REWRITE_ROUNDS", "5"))
_DEFAULT_MAX_TOKENS = 4096

# PR-CH-TRUNCATION (2026-06-29): scene_writer sometimes hits its max_tokens
# ceiling; the OpenAI-compat stream ends mid-sentence and model_router's
# generate_stream never inspects finish_reason, so the partial scene is appended
# and the chapter is saved "completed" while ending on a dangling half-sentence.
# A finished Chinese chapter ends on sentence-terminal punctuation. Anything else
# on the final non-empty line means the tail was cut off.
_SENTENCE_TERMINALS = "。！？…”』」）】》—"
_TRAILING_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*$")


def looks_truncated(text: str) -> bool:
    """Return True when the chapter body appears cut off mid-sentence.

    A complete Chinese chapter ends on terminal punctuation (。！？…) possibly
    followed by a closing quote/bracket. We strip trailing blank lines and a
    stray closing code fence, then check the last non-empty line's final char.
    Empty/blank text is NOT flagged here (that's a separate empty-output path).
    """
    if not text or not text.strip():
        return False
    cleaned = _TRAILING_FENCE_RE.sub("", text).strip()
    if not cleaned:
        return False
    last_char = cleaned[-1]
    return last_char not in _SENTENCE_TERMINALS


# PR-CH-REFUSAL (2026-07-04): relay occasionally misroutes a scene_writer text
# call to an image model, which replies with assistant/service refusal
# boilerplate (e.g. "我可以搜索图片，但目前似乎无法为您创建任何图片"). The
# orchestrator appends it as a scene; because it ends on 「。」 it slips past
# looks_truncated and the chapter is saved "completed" carrying a refusal
# instead of prose. These are DISTINCTIVE multi-word phrases that never occur
# in real narrative — single tokens like "图片" are deliberately excluded so
# legitimate prose ("她翻看着旧图片") is not flagged.
_REFUSAL_PHRASES = (
    "无法为您创建任何图片",
    "尚未开通图片创建功能",
    "无法为您生成图片",
    "我无法创建图片",
    "我不能生成图片",
)


def looks_like_refusal(text: str) -> bool:
    """Return True when the text contains known assistant/service refusal
    boilerplate (typically an image-model misroute) rather than story prose.

    Matches distinctive multi-word phrases only, so ordinary prose that merely
    mentions 图片/登录 is not flagged. Empty/blank text is NOT flagged.
    """
    if not text or not text.strip():
        return False
    return any(phrase in text for phrase in _REFUSAL_PHRASES)


_LEXICAL_CLEANUP_REPLACEMENTS = {
    "半寸": "一点",
    "半息": "一瞬",
    "半指": "一点",
    "寸许": "少许",
    "尺许": "一段",
    "肘下": "身侧",
    "腋下": "臂弯里",
    "把手机按黑": "锁屏",
    "按黑": "锁屏",
    "别挡锅": "别挡着锅",
    "别碰那边": "别过去",
    "别进楼": "别往楼里跑",
    "带了急": "急了",
}

QUALITY_REWRITE_BAD_EXAMPLE = (
    "“踩烂了，算你买。”\n"
    "“先问问它算谁卖。”\n"
    "“你别装了。”\n"
    "“我装什么？”\n"
    "“你刚才说看一行，现在又想看，你就是想赖账。”\n\n"
    "邱成把笔记本放回柜台，手还压在上面。\n"
    "“说好一行。”\n"
    "“就一行。”\n"
    "“只露一行。”\n"
    "“看一行。”\n"
    "“看一行。”\n"
    "“多露一个字呢？”\n"
    "“你自己合上。”\n"
    "“认错呢？”\n"
    "“赔礼，再补湿损纸钱。”\n"
    "“认对呢？”\n"
    "“明早我带见证到市书会认旧物。”\n\n"
    "陈青往前挪了一步，脚尖仍停在柜台影子外。\n"
    "“别过线。”\n"
    "“我没过。”\n"
    "“念。”\n"
    "“青儿药照前方，德安堂欠二钱。”\n"
    "“街上药单都这么写。”\n"
    "“德安堂柜上不这么写。”\n"
    "“你又懂德安堂？”\n"
    "“六岁到十六岁，我替陈玉枝跑德安堂。老周记账，先写月份，再写病人，再写药钱。怕抓错药，家里人才把‘青儿’写在前头。”\n\n"
    "便利店里的男人说：“执行者都过去了，听说是血裔闹出来的，旧神那边都翻成这样了，奥丁那条线你也敢碰。”\n\n"
    "左边一排旧信箱都合着口，右墙贴着张褪色通知。东头有连廊，西头是楼梯口。"
)

QUALITY_REWRITE_GOOD_EXAMPLE = (
    "“脚底留神，踩坏了照价赔。”\n"
    "“往后退，地上都是货。”\n"
    "“没瞎就看着点脚下。”\n\n"
    "邱成手背青筋微突，死死按住本子：“只露一行。多半个字，我立马撕了。”\n\n"
    "“可以。”陈青没废话，“认错我赔钱。认对，这本东西今晚谁也别想碰，明早市书会见。”\n\n"
    "陈青逼近半步。\n\n"
    "邱成眼神瞬间警惕：“退回去。”\n\n"
    "陈青没退，目光盯死那行字：“‘青儿药照前方，德安堂欠二钱’。”\n\n"
    "“街上药单都这么写，算什么铁证？”\n\n"
    "“德安堂老周记账，规矩是年月打头，病人在后。”陈青声音冷硬，"
    "“只有我祖母去赊药，怕老眼昏花抓错，才会把我的名字强行顶在最前头。”\n\n"
    "男人把找零攥进手里，骂骂咧咧：“南环那片又拉网了，说是刚过去两辆黑车。这大雨天的，还让不让人活？”\n"
    "收银台后的女人头都没抬：“查得紧？”\n"
    "“说是上面有人发疯。那帮不人不鬼的东西一折腾，咱们连路都走不通。”\n"
    "“闭嘴。你想把清道的人招进来查我的店，就接着嚷。”\n\n"
    "门板是烂透的旧铁皮，旁边却死死钉着一块崭新的金属门牌，亮得刺眼。门没锁，扣舌一送，里头露出来的楼道干得发亮。"
)


@dataclass
class ChapterQualityGateResult:
    status: QualityGateStatus
    original_text: str
    final_text: str
    initial_report: ChineseProseMechanicsReport
    final_report: ChineseProseMechanicsReport
    rewrite_rounds: int = 0
    rewrite_attempts: list[dict[str, Any]] = field(default_factory=list)
    warning_reason: str | None = None
    target_word_count: int | None = None
    min_target_word_count: int | None = None

    def to_safe_metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rewrite_rounds": self.rewrite_rounds,
            "warning_reason": self.warning_reason,
            "target_word_count": self.target_word_count,
            "min_target_word_count": self.min_target_word_count,
            "initial_report": self.initial_report.to_safe_dict(),
            "final_report": self.final_report.to_safe_dict(),
            "rewrite_attempts": [
                {
                    "round": item.get("round"),
                    "accepted": item.get("accepted"),
                    "passed": item.get("passed"),
                    "candidate_len": item.get("candidate_len"),
                    "score": item.get("score"),
                    "reason": item.get("reason"),
                }
                for item in self.rewrite_attempts
            ],
        }

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def _normalize_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^---+\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = apply_mechanical_lexical_cleanup(text)
    return text.strip()


def apply_mechanical_lexical_cleanup(text: str) -> str:
    """Remove fixed prose-mechanics watchlist terms after LLM rewrite.

    The LLM often fixes dialogue rhythm but leaves one or two exact lexical
    watchlist terms. These replacements are local and meaning-preserving enough
    to run deterministically before the final report/save decision.
    """
    cleaned = text or ""
    for source, target in _LEXICAL_CLEANUP_REPLACEMENTS.items():
        cleaned = cleaned.replace(source, target)
    return cleaned


def _quality_penalty(report: ChineseProseMechanicsReport) -> int:
    return (
        report.forbidden_collocation_count * 1000
        + report.space_watchlist_hits * 400
        + report.spatial_mapping_count * 380
        + report.dialogue_symmetry_risk_count * 320
        + report.duplicate_short_dialogue_ladder_count * 320
        + report.biographical_infodump_count * 300
        + report.prop_fiddling_count * 250
        + report.explicit_pause_marker_count * 200
        + report.direct_intent_exposition_count * 220
        + report.motive_exposition_count * 240
        + report.cheap_wit_count * 280
        + report.perfect_comeback_run_count * 300
        + report.action_quote_paragraph_count * 35
        + int(max(0.0, report.action_quote_paragraph_rate - 0.35) * 1000)
        + report.tight_qa_pair_count * 60
        + int(max(0.0, report.short_dialogue_density - 0.45) * 700)
        + int(max(0.0, report.short_paragraph_density - 0.35) * 500)
        + report.procedural_exposition_cluster_count * 260
        + report.story_bible_leakage_count * 360
        + report.meta_structure_leakage_count * 500
        + report.directional_listing_count * 260
        + report.mundane_logic_violation_count * 420
        + report.awkward_register_count * 260
        + report.limited_pov_leak_count * 320
        + report.hardship_stack_count * 260
        + report.resource_continuity_count * 420
        + report.scene_plausibility_count * 360
        + report.action_causality_count * 360
        + report.mundane_register_count * 260
        + report.motivation_gap_count * 420
        + report.transport_logic_count * 420
        + report.semantic_collocation_count * 320
        + report.shelter_cost_logic_count * 420
        + report.abstract_evasion_count * 320
        + report.pseudo_literary_register_count * 320
        + int(report.interiority_monologue_rate * 400)
        + report.repeated_realization_run * 60
        + int(max(0.0, 0.10 - report.dialogue_paragraph_rate) * 300)
        + report.plain_contemporary_violation_count * 360
        + report.duplicate_explanation_span_count * 300
        + max(0, report.action_dialogue_beat_count - 14) * 120
        + report.short_sentence_runs_over_target * 80
        + max(0, report.short_sentence_run_max - 8) * 40
        + report.exposition_cluster_risk * 60
        + report.long_quote_segments_gt80 * 50
        + report.duplicate_quote_segments_gt20 * 50
    )


def _candidate_is_acceptable(
    *,
    original_text: str,
    candidate_text: str,
    candidate_report: ChineseProseMechanicsReport,
    best_report: ChineseProseMechanicsReport,
    best_text: str,
    target_word_count: int | None = None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
) -> tuple[bool, str | None]:
    if not candidate_text.strip():
        return False, "empty"

    original_len = len(original_text)
    candidate_len = len(candidate_text)
    min_len = max(1, int(original_len * _MIN_LENGTH_RATIO))
    if candidate_len < min_len:
        return False, f"too_short<{_MIN_LENGTH_RATIO:.2f}"
    if _target_length_shortfall(
        candidate_text,
        target_word_count=target_word_count,
        min_target_word_ratio=min_target_word_ratio,
    ):
        return False, "target_length_shortfall"

    if candidate_report.passed:
        return True, None

    candidate_score = _quality_penalty(candidate_report)
    best_score = _quality_penalty(best_report)
    if candidate_score < best_score:
        return True, None
    if candidate_score == best_score and candidate_len >= len(best_text):
        return True, None
    return False, "no_improvement"


def _min_target_word_count(
    target_word_count: int | None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
) -> int | None:
    if not target_word_count or int(target_word_count) <= 0:
        return None
    return max(1, int(int(target_word_count) * float(min_target_word_ratio)))


def _target_length_shortfall(
    text: str,
    *,
    target_word_count: int | None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
) -> bool:
    minimum = _min_target_word_count(target_word_count, min_target_word_ratio)
    if minimum is None:
        return False
    return len((text or "").strip()) < minimum


def _build_rewrite_user_content(
    *,
    text: str,
    initial_report: ChineseProseMechanicsReport,
    round_idx: int,
    previous_attempts: list[dict[str, Any]],
) -> str:
    payload = {
        "pass": initial_report.passed,
        "action_dialogue_beat_count": initial_report.action_dialogue_beat_count,
        "prop_fiddling_count": initial_report.prop_fiddling_count,
        "explicit_pause_marker_count": initial_report.explicit_pause_marker_count,
        "direct_intent_exposition_count": initial_report.direct_intent_exposition_count,
        "motive_exposition_count": initial_report.motive_exposition_count,
        "cheap_wit_count": initial_report.cheap_wit_count,
        "perfect_comeback_run_count": initial_report.perfect_comeback_run_count,
        "short_sentence_run_max": initial_report.short_sentence_run_max,
        "short_sentence_runs_over_target": initial_report.short_sentence_runs_over_target,
        "forbidden_collocation_count": initial_report.forbidden_collocation_count,
        "dialogue_symmetry_risk_count": initial_report.dialogue_symmetry_risk_count,
        "duplicate_short_dialogue_ladder_count": initial_report.duplicate_short_dialogue_ladder_count,
        "spatial_mapping_count": initial_report.spatial_mapping_count,
        "biographical_infodump_count": initial_report.biographical_infodump_count,
        "paragraph_count": initial_report.paragraph_count,
        "quote_count": initial_report.quote_count,
        "action_quote_paragraph_count": initial_report.action_quote_paragraph_count,
        "action_quote_paragraph_rate": initial_report.action_quote_paragraph_rate,
        "tight_qa_pair_count": initial_report.tight_qa_pair_count,
        "short_dialogue_density": initial_report.short_dialogue_density,
        "short_paragraph_density": initial_report.short_paragraph_density,
        "procedural_exposition_cluster_count": initial_report.procedural_exposition_cluster_count,
        "story_bible_leakage_count": initial_report.story_bible_leakage_count,
        "meta_structure_leakage_count": initial_report.meta_structure_leakage_count,
        "directional_listing_count": initial_report.directional_listing_count,
        "awkward_register_count": initial_report.awkward_register_count,
        "limited_pov_leak_count": initial_report.limited_pov_leak_count,
        "mundane_logic_violation_count": initial_report.mundane_logic_violation_count,
        "hardship_stack_count": initial_report.hardship_stack_count,
        "resource_continuity_count": initial_report.resource_continuity_count,
        "mundane_register_count": initial_report.mundane_register_count,
        "action_causality_count": initial_report.action_causality_count,
        "motivation_gap_count": initial_report.motivation_gap_count,
        "scene_plausibility_count": initial_report.scene_plausibility_count,
        "transport_logic_count": initial_report.transport_logic_count,
        "semantic_collocation_count": initial_report.semantic_collocation_count,
        "shelter_cost_logic_count": initial_report.shelter_cost_logic_count,
        "abstract_evasion_count": initial_report.abstract_evasion_count,
        "pseudo_literary_register_count": initial_report.pseudo_literary_register_count,
        "plain_contemporary_violation_count": initial_report.plain_contemporary_violation_count,
        "duplicate_explanation_span_count": initial_report.duplicate_explanation_span_count,
        "interiority_monologue_rate": round(initial_report.interiority_monologue_rate, 3),
        "repeated_realization_run": initial_report.repeated_realization_run,
        "dialogue_paragraph_rate": round(initial_report.dialogue_paragraph_rate, 3),
    }
    return (
        "请重写下面这段中文小说正文，只修复机械痕迹，不要改变事件顺序、人物关系和核心信息。"
        "密集交锋里允许连续纯台词，动作只保留真正改变局势的一拍。"
        "不要输出解释、分析、标题、Markdown、项目符号或前言。"
        "字数不要明显缩短，尽量接近原文。\n\n"
        f"【第 {round_idx} 轮改写目标】\n"
        "- 删除节拍器式的动作+台词配对。\n"
        "- 删除无意义的道具摆弄。\n"
        "- 删除显性停顿词。\n"
        "- 藏住潜台词，不要直接拆穿对方真实意图。\n\n"
        "- 删除廉价机智与翻译腔抛梗；日常护财、试探、讨价还价要服从情绪，不要把粗鄙护财写成聪明反击。\n"
        "- 破坏排比式短问短答；连续短句交锋必须加入答非所问、抢白、动作打断或直接抛结论。\n"
        "- 删除三步外、一指宽、影子外、几寸、一尺、一丈、几步、几尺、几丈等静态物理测绘词；半步只可用于逼近/退开等动态压迫。\n"
        "- 删除履历式自白；往事作证最多两句，不许从几岁讲到几岁。\n\n"
        "- meta_structure_leakage_zero：删除正文中的第X章/第X卷/[CH-n]/本章开始/本章完等元结构标签；章节编号不得进入小说正文。\n\n"
        "- setting_name_dialogue_zero：删除设定名朗读。路人、新闻、店员、邻居、广告、海报和闲聊不得讨论执行者、血裔、旧神、奥丁等核心名词；改成封路、停电、绕路、物价、黑车、查得紧、上面、那帮人、那种事、清道等生活影响和代词。\n"
        "- directional_listing_zero：删除左边/右边/东头/西头/前后导览式罗列；环境只抓一个与氛围或剧情冲突的核心反差点。\n\n"
        "- mundane_scene_plausibility：修正不合生活逻辑的日常场景。烤肠摊只卖摊位合理物，不顺手出现临期面包、热水、汤锅；便利店不硬塞消毒水味和温水；大雨院子里不要安排人群围电视。\n"
        "- plain_modern_register：改回正常现代汉语。写锁屏/按灭屏幕、别挡着锅、别往楼里跑/别进去，不写把手机按黑、别挡锅、别碰那边、带了急、收租截图这类压缩拗口词。\n"
        "- plain_contemporary_chinese：普通现代场景必须用完整、自然、当代的中文表达，不要写半文言、伪文学、为了显得干练而硬压缩的句子。写“他叫了一声：‘师傅。’”“干嘛？”“车没了，手机快没电了”，不要写“喊了声师傅”“来意说得很低”“终于抬头”“声音被关门声挡住一半”。\n"
        "- limited_pov_only：第三人称有限视角只保留主角能看见、听见、感到的事实；删除“才想起他、没人记得、没人问他、他像水印一样会没”这类替别人下心理结论或点破比喻的旁白。\n"
        "- semantic_density_budget：开篇苦难/边缘化信息限量，只留两三个与当前困境物理相关的痛点，删除老师、门卫、班群、食堂、房东同质标签堆叠。\n\n"
        "- resource_continuity：修正钱、手机、电量、支付方式、交通选择之间的矛盾；叫车失败、现金不足、移动支付、旁人坐车都必须有一致资源逻辑。\n"
        "- action_causality：修正动作和结果不成立的问题；不要写挡着锅却雨水进锅、香味直接导致胃更空、看到灯就自动进门。\n"
        "- motivation_bridge：角色进入陌生建筑、异常门或私人空间前，必须补足压力、退路关闭、误判理由、求生诱因或外部催逼。\n\n"
        "- transport_logic：修正公交、地铁、网约车和步行链路矛盾；末班车已经没了就不要再写最近换乘赶不上，改成夜班线路绕远、封路、叫车失败、余额不足或时间不够。\n"
        "- semantic_collocation：修正不完整或故作深的现代汉语搭配；写透不出灯光/没有灯光，不写透不出灯；写价格超过余额，不写价格难看；写没有人声/没有开门动静，不写可以借口留下来的声音。\n"
        "- shelter_cost_logic：不住旅馆/宾馆/酒店的原因必须落在房费、押金、余额、满房、证件、关门、风险或距离上；不要用背包放干、没力气碰脸色、从头看到脚当核心理由。\n"
        "- abstract_evasion：删除试错、借口留下来、价格难看、碰这种脸色等抽象逃避词，把行为理由写成钱、时间、电量、体力、路况和退路。\n\n"
        "- cross_project_prose_quality_contract：先按规则族修，不要只替换用户点名的一句话。同类伪文学压缩腔、语义搭配缺失、资源逻辑断裂和重复解释都要一起清理。\n"
        "- duplicate_explanation_control：同一压力链只解释一次。重复的退路说明、心理金句和困境标签要删除或改成下一步行动。\n\n"
        "- chapter_level_anti_padding：每段必须带新信息（动作/对话/发现/关系变化）；同一洞察全章只写一次，禁止换比喻反复重述；删除连续堆叠的心理剖白段，段落不得只复述上一段结论；对话与有信息的叙述/动作要平衡，不要用独白金句凑字数，也不要用整章短对白墙或一句一段凑字数。\n\n"
        "- 降低动作对白绑定率；不要让多数段落都是人物摆一下、看一下、挪一下之后说一句话。\n"
        "- 降低紧贴问答；问题不能被下一句完美接住，必须加入无视、抢白、答非所问、说半截、证物或环境打断。\n"
        "- 降低短对白密度和短段落密度；把同一动作、同一证据链、同一轮施压合并成自然段，保留呼吸。\n"
        "- 拆散程序性解释簇；旧库、码头、灯籍、封存、回封、待验等制度/证据说明不得由专家 NPC 一口气讲完。\n\n"
        "- 尤其禁止复现：说好一行 / 就一行 / 多看一个字呢 / 你自己合上 / 认错呢 / 认对呢 / 明早我带见证到市书会认旧物。这类模板必须改成抢白、打断、直接抛结论或由证物打断。\n\n"
        "【结构指标说明】\n"
        "动作对白绑定率=带动作调度的对白段落/总段落；紧贴问答=一个问句后立刻被下一句接住；"
        "短对白密度=短对白占全部对白比例；短段落密度=短段落占全部段落比例；"
        "程序性解释簇=制度/证据流程词在长段中密集出现。上述指标偏高时，即使没有黑名单词，也必须重写。\n\n"
        "【坏样例】\n"
        f"{QUALITY_REWRITE_BAD_EXAMPLE}\n\n"
        "【好样例】\n"
        f"{QUALITY_REWRITE_GOOD_EXAMPLE}\n\n"
        "【当前检测摘要】\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "【上一轮结果】\n"
        f"{json.dumps(previous_attempts, ensure_ascii=False, indent=2)}\n\n"
        "【待改写正文】\n"
        f"{text}"
    )


async def apply_chapter_quality_gate(
    *,
    text: str,
    db: AsyncSession,
    project_id: str | None = None,
    chapter_id: str | None = None,
    skip_polish: bool = False,
    max_rewrite_rounds: int = _DEFAULT_MAX_REWRITE_ROUNDS,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    target_word_count: int | None = None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
) -> ChapterQualityGateResult:
    """Inspect generated chapter prose and optionally rewrite it.

    The gate returns the best available candidate. It never raises for bad prose
    alone; callers decide whether to persist the returned text and emit warning
    events.
    """
    original_text = text or ""
    initial_report = analyze_chinese_prose_mechanics(original_text)
    min_target = _min_target_word_count(target_word_count, min_target_word_ratio)

    if project_id and original_text:
        try:
            from app.models.project import Character
            from sqlalchemy import select

            stmt = select(Character.name).where(Character.project_id == project_id)
            res = await db.execute(stmt)
            char_names = [row[0] for row in res.all() if row[0]]
            for name in char_names:
                if name not in original_text:
                    logger.warning(
                        "quality_gate missing_char project=%s char=%s",
                        project_id, name,
                    )
        except Exception:
            pass

    if skip_polish:
        return ChapterQualityGateResult(
            status="skipped",
            original_text=original_text,
            final_text=original_text,
            initial_report=initial_report,
            final_report=initial_report,
            target_word_count=target_word_count,
            min_target_word_count=min_target,
        )

    if initial_report.passed:
        if _target_length_shortfall(
            original_text,
            target_word_count=target_word_count,
            min_target_word_ratio=min_target_word_ratio,
        ):
            return ChapterQualityGateResult(
                status="blocked",
                original_text=original_text,
                final_text=original_text,
                initial_report=initial_report,
                final_report=initial_report,
                warning_reason="target_length_shortfall",
                target_word_count=target_word_count,
                min_target_word_count=min_target,
            )
        return ChapterQualityGateResult(
            status="passed",
            original_text=original_text,
            final_text=original_text,
            initial_report=initial_report,
            final_report=initial_report,
            target_word_count=target_word_count,
            min_target_word_count=min_target,
        )

    current_text = original_text
    current_report = initial_report
    best_text = original_text
    best_report = initial_report
    attempts: list[dict[str, Any]] = []
    warning_reason: str | None = None

    extra_system = contract_hard_gate_prompt()

    for round_idx in range(1, max(0, max_rewrite_rounds) + 1):
        user_content = _build_rewrite_user_content(
            text=current_text,
            initial_report=current_report,
            round_idx=round_idx,
            previous_attempts=attempts,
        )
        logger.info(
            "chapter_quality_gate rewrite start project_id=%s chapter_id=%s round=%d penalty=%d",
            project_id,
            chapter_id,
            round_idx,
            _quality_penalty(current_report),
        )
        result = await run_text_prompt(
            task_type="rewrite",
            user_content=user_content,
            db=db,
            extra_system=extra_system,
            project_id=project_id,
            chapter_id=chapter_id,
            max_tokens=max_tokens,
        )
        candidate_text = _normalize_text(result.text or "")
        candidate_report = analyze_chinese_prose_mechanics(candidate_text)
        accepted, reason = _candidate_is_acceptable(
            original_text=original_text,
            candidate_text=candidate_text,
            candidate_report=candidate_report,
            best_report=best_report,
            best_text=best_text,
            target_word_count=target_word_count,
            min_target_word_ratio=min_target_word_ratio,
        )
        attempt_record = {
            "round": round_idx,
            "candidate_len": len(candidate_text),
            "passed": candidate_report.passed,
            "score": _quality_penalty(candidate_report),
            "accepted": accepted,
            "reason": reason,
            "report": candidate_report.to_safe_dict(),
        }
        attempts.append(attempt_record)

        if accepted:
            best_text = candidate_text
            best_report = candidate_report
            current_text = candidate_text
            current_report = candidate_report
            if candidate_report.passed:
                return ChapterQualityGateResult(
                    status="passed",
                    original_text=original_text,
                    final_text=best_text,
                    initial_report=initial_report,
                    final_report=best_report,
                    rewrite_rounds=round_idx,
                    rewrite_attempts=attempts,
                    target_word_count=target_word_count,
                    min_target_word_count=min_target,
                )
            warning_reason = "improved_but_not_passed"
        else:
            warning_reason = reason or "rejected"

    return ChapterQualityGateResult(
        status="blocked",
        original_text=original_text,
        final_text=best_text,
        initial_report=initial_report,
        final_report=best_report,
        rewrite_rounds=len(attempts),
        rewrite_attempts=attempts,
        warning_reason=warning_reason or "quality_gate_blocked",
        target_word_count=target_word_count,
        min_target_word_count=min_target,
    )
