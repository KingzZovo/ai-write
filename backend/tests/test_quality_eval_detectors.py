"""确定性检测器的正向/反向用例。

每个探测器都必须证明两件事:命中真实缺陷时报警,以及措辞正常时不误报。
检测器只做"命中提示",但误报会污染前后对照的数字,所以反向用例与正向用例同等重要。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.quality_eval import detectors  # noqa: E402
from scripts.quality_eval.cases import Case, Requirement, load_case_set  # noqa: E402

CASE_SET = BACKEND_ROOT / "scripts" / "quality_eval" / "cases" / "sample_set_v1.json"


def make_case(**overrides) -> Case:
    base = dict(
        id="unit_case",
        label="单元",
        genre="单元",
        pov="第三人称限知,跟随甲",
        pov_character="甲",
        cast=("甲", "乙"),
        target_words=1000,
        word_range=(850, 1250),
        brief="",
        must_keep=(),
        forbidden=(),
        required_outcomes=(),
        mechanism_terms=(),
        numbers_allowed=(),
        ending_required_any=(),
    )
    base.update(overrides)
    return Case(**base)


def categories(det: detectors.Detection, name: str) -> list[dict]:
    return [f for f in det.to_dict()["findings"] if f["category"] == name]


# ---------------------------------------------------------------------------
# 1. 视角越界
# ---------------------------------------------------------------------------

def test_pov_breach_flags_other_character_interiority():
    case = make_case()
    text = "甲把碗推过去。乙心里明白这顿饭的意思。"
    hits = categories(detectors.detect(case, text), "pov_breach")
    assert any(h["detector"] == "interior_of_non_pov" for h in hits)
    hit = next(h for h in hits if h["detector"] == "interior_of_non_pov")
    assert hit["offset"] == text.index("乙心里")


def test_pov_breach_allows_pov_interiority_and_external_detail():
    case = make_case()
    text = "甲心里明白,这顿饭的意思不简单。乙把碗推过去,指节发白。"
    hits = categories(detectors.detect(case, text), "pov_breach")
    assert hits == []


def test_pov_breach_flags_unauthorized_camera_shift():
    case = make_case()
    hits = categories(detectors.detect(case, "与此同时,城里的人正等着回话。"), "pov_breach")
    assert [h["detector"] for h in hits] == ["omniscient_camera_shift"]


# ---------------------------------------------------------------------------
# 2. 写作指令泄漏
# ---------------------------------------------------------------------------

def test_instruction_leak_flags_planning_field_and_markdown():
    case = make_case()
    det = detectors.detect(case, '他必须用行动来展示断桥的阻力。\n```json\n{"start_state": "无"}\n```')
    hits = categories(det, "instruction_leak")
    detectors_found = {h["detector"] for h in hits}
    assert "writing_instruction" in detectors_found
    assert "planning_field" in detectors_found
    assert all(h["severity"] == "hard" for h in hits if h["detector"] == "planning_field")


def test_instruction_leak_allows_plain_narration():
    case = make_case()
    text = "他数了数口袋里的钱,把信重新掖好,抬脚往桥头走。"
    assert categories(detectors.detect(case, text), "instruction_leak") == []


# ---------------------------------------------------------------------------
# 3. 规则复述
# ---------------------------------------------------------------------------

def test_rule_recitation_flags_expository_rule_sentence():
    case = make_case(mechanism_terms=("铜铃", "只够一个人"))
    text = "铜铃只够一个人走,烧毁后就不可恢复。他抓了抓腰上的布袋。"
    hits = categories(detectors.detect(case, text), "rule_recitation")
    assert hits, "并列机制词且无动作应命中"
    assert hits[0]["quote"].startswith("铜铃只够一个人走")


def test_rule_recitation_allows_mechanism_in_action_and_dialogue():
    case = make_case(mechanism_terms=("铜铃",))
    text = "他一把摇响铜铃,水路从脚底铺开,他踏上去就往前走。"
    assert categories(detectors.detect(case, text), "rule_recitation") == []
    dialogue = "“铜铃只能响一次。”他说完就把铃攥紧了。"
    assert categories(detectors.detect(case, dialogue), "rule_recitation") == []
def test_rule_recitation_does_not_flag_mere_repetition():
    """主题词重复不是复述:合同本身允许准确名称重复,所以这里必须不报。"""
    case = make_case(mechanism_terms=("铜铃",))
    text = "铜铃响了。他摸到铜铃。铜铃发烫。他攥着铜铃。铜铃碎了。"
    assert categories(detectors.detect(case, text), "rule_recitation") == []


def test_rule_recitation_flags_definitional_exposition():
    """一句里并列两个机制词并带解释框架 = 把设定讲一遍。"""
    case = make_case(mechanism_terms=("走地根", "三年陈醋"))
    text = "那是用来封存走地根的三年陈醋,平日里金贵得很。"
    hits = categories(detectors.detect(case, text), "rule_recitation")
    assert any(h["detector"] == "mechanism_exposition" for h in hits)


def test_rule_recitation_ignores_action_sentences_naming_two_terms():
    """只是同时提到两个东西、但没有任何解释框架的句子不算复述。"""
    case = make_case(mechanism_terms=("腰牌", "铁索"))
    text = "既然老周咬死不收腰牌,赊账无门,他只能咬牙转向那两根仅存的铁索。"
    assert categories(detectors.detect(case, text), "rule_recitation") == []

# ---------------------------------------------------------------------------
# 4. 比喻堆叠
# ---------------------------------------------------------------------------

def test_metaphor_stack_flags_dense_paragraph():
    case = make_case()
    text = "河水像野兽般咆哮,声音如同低沉的雷鸣,机会犹如潮水般流失在指缝里。"
    hits = categories(detectors.detect(case, text), "metaphor_stack")
    assert any(h["detector"] == "paragraph_simile_density" for h in hits)


def test_metaphor_stack_ignores_false_friends():
    case = make_case()
    text = "如果他今天不来,那么如今这笔账还要再等,比如上周就是这样。如此反复,他已经习惯。"
    assert categories(detectors.detect(case, text), "metaphor_stack") == []


# ---------------------------------------------------------------------------
# 5. 结果兑现 / 结尾
# ---------------------------------------------------------------------------

def test_outcome_unfulfilled_flags_missing_result():
    case = make_case(required_outcomes=(Requirement(id="pay", desc="交钱", tokens_any=("付了", "递过去")),))
    hits = categories(detectors.detect(case, "他坐了一会儿,把碗推开。"), "outcome_unfulfilled")
    assert [h["detector"] for h in hits] == ["outcome:pay"]


def test_outcome_unfulfilled_passes_when_all_requirements_met():
    case = make_case(
        required_outcomes=(Requirement(id="pay", desc="交钱", tokens_any=("递过去",)),),
        ending_required_any=("上船",),
    )
    text = "他把钱递过去,拎起包走上船。"
    assert categories(detectors.detect(case, text), "outcome_unfulfilled") == []


def test_outcome_unfulfilled_flags_intent_only_ending():
    case = make_case()
    text = "他把碗推开。钱的事明天再谈,他打算以后再还。"
    hits = categories(detectors.detect(case, text), "outcome_unfulfilled")
    assert any(h["detector"] == "ending_intent_only" for h in hits)


# ---------------------------------------------------------------------------
# 6. 事实改动
# ---------------------------------------------------------------------------

def test_fact_violation_flags_forbidden_addition_and_missing_keep():
    case = make_case(
        must_keep=(Requirement(id="debt", desc="欠三千", tokens_all=("三千",)),),
        forbidden=(Requirement(id="gift", desc="不得他人送钱", tokens_any=("张哥",)),),
    )
    hits = categories(detectors.detect(case, "张哥说不用还了。"), "fact_violation")
    found = {h["detector"] for h in hits}
    assert "must_keep:debt" in found
    assert "forbidden:gift" in found


def test_fact_violation_allows_declared_numbers():
    case = make_case(numbers_allowed=("三千", "两千"))
    assert categories(detectors.detect(case, "他还欠三千,今天先拿两千。"), "fact_violation") == []


def test_fact_violation_flags_undeclared_quantity():
    case = make_case(numbers_allowed=("三千",))
    hits = categories(detectors.detect(case, "他掏出五百元放在桌上。"), "fact_violation")
    assert [h["detector"] for h in hits] == ["unexpected_quantity"]
    assert hits[0]["severity"] == "soft"


# ---------------------------------------------------------------------------
# 可重复性与指标
    assert case.target_words == int(case.target_words)



# ---------------------------------------------------------------------------
# 7. 未授权人物
# ---------------------------------------------------------------------------

def _relation_case(brief: str, cast: tuple[str, ...] = ("甲", "乙")) -> Case:
    return make_case(
        brief=brief,
        cast=cast,
        pov_character=cast[0],
        must_keep=(Requirement(id="k", desc="占位", tokens_any=("甲",)),),
        forbidden=(Requirement(id="f", desc="占位", tokens_any=("禁止词",)),),
        required_outcomes=(Requirement(id="o", desc="占位", tokens_any=("甲",)),),
    )


def test_invented_relation_flags_unset_family_member():
    """作者只写了母亲和女儿,正文里冒出「你哥」= 凭空补了一个家庭成员。"""
    case = _relation_case("母亲和女儿在摊前说话。", cast=("李巧云", "陈麦"))
    hits = [f for f in detectors.detect(case, "“你哥现在退了学,拿什么翻身?”").to_dict()["findings"]
            if f["detector"] == "invented_relation"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "hard"


def test_invented_relation_allows_authorized_family_member():
    case = _relation_case("母亲和女儿在摊前说话。", cast=("李巧云", "陈麦"))
    text = "“妈,你听我说。”陈麦抬起头。她妈没接话。"
    assert [f for f in detectors.detect(case, text).to_dict()["findings"]
            if f["detector"] == "invented_relation"] == []


def test_invented_relation_ignores_generic_nouns_with_relation_characters():
    """「姑娘」「老孙家的媳妇」不是主角亲属,不能因为含「娘」「媳妇」就报警。"""
    case = _relation_case("药铺学徒阿棠要处理半坛陈醋。", cast=("阿棠", "柳先生"))
    text = "“阿棠姑娘,行行好!”那是巷尾老孙家的媳妇,昨夜已经来过两回。"
    assert [f for f in detectors.detect(case, text).to_dict()["findings"]
            if f["detector"] == "invented_relation"] == []


def test_unauthorized_new_character_flags_titled_stranger():
    case = _relation_case("周芸要凑两千元还给弟弟周航。", cast=("周芸", "周航"))
    text = "我明天一早去找供销社的张阿姨,她手里兴许有活钱。"
    hits = [f for f in detectors.detect(case, text).to_dict()["findings"]
            if f["detector"] == "unauthorized_new_character"]
    assert [h["detail"] for h in hits] and "张阿姨" in hits[0]["detail"]


def test_unauthorized_new_character_allows_known_cast_with_title():
    case = _relation_case("苏姐让伙计阿六去前堂看茶。", cast=("苏姐", "阿六"))
    text = "“阿六,去把火看好。”苏姐说完就走。"
    assert [f for f in detectors.detect(case, text).to_dict()["findings"]
            if f["detector"] == "unauthorized_new_character"] == []


def test_unauthorized_new_character_ignores_ordinary_verb_phrases():
    """「林叶没说话」「周芸点点头」这类跨词边界匹配不能算新人物。"""
    case = _relation_case("林叶去救许舟。两人同行三年。", cast=("林叶", "许舟"))
    text = "林叶没说话,解下腰间的面饼。周芸点点头,又把手插回围裙兜里。"
    assert [f for f in detectors.detect(case, text).to_dict()["findings"]
            if f["detector"] == "unauthorized_new_character"] == []
def test_detection_is_deterministic_and_offsets_are_usable():
    case_set = load_case_set(CASE_SET)
    case = case_set.by_id("real_ferry")
    text = "周芸把信封压住。周航心里明白,这点钱不够。他必须用行动来展示难处。\n张哥说不用还了。"
    first = detectors.detect(case, text)
    second = detectors.detect(case, text)
    assert json.dumps(first.to_dict(), ensure_ascii=False, sort_keys=True) == json.dumps(
        second.to_dict(), ensure_ascii=False, sort_keys=True
    )
    for finding in first.to_dict()["findings"]:
        if finding["offset"] >= 0:
            assert 0 <= finding["offset"] < len(text)


def test_metrics_report_language_surface():
    case = make_case()
    det = detectors.detect(case, "他说:“走吧。”然后他走了。")
    metrics = det.to_dict()["metrics"]
    assert metrics["chars"] > 0
    assert 0 <= metrics["dialogue_ratio"] <= 1
    assert metrics["sentences"] >= 1


def test_sample_set_covers_three_genres_with_two_scenes_each():
    case_set = load_case_set(CASE_SET)
    by_genre: dict[str, int] = {}
    for case in case_set.cases:
        by_genre[case.genre] = by_genre.get(case.genre, 0) + 1
    assert len(by_genre) >= 3
    assert all(n >= 2 for n in by_genre.values())


@pytest.mark.parametrize("case_id", ["real_ferry", "real_stall", "fantasy_bell", "fantasy_herb",
                                     "mystery_bridge", "mystery_lodger"])
def test_every_case_declares_checkable_contract(case_id):
    case = load_case_set(CASE_SET).by_id(case_id)
    assert case.brief.strip()
    assert case.must_keep and case.forbidden and case.required_outcomes
    for group in (case.must_keep, case.forbidden, case.required_outcomes):
        for req in group:
            assert req.tokens_all or req.tokens_any
