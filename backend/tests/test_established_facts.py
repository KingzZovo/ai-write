"""已确立事实:抽取、注入、确定性一致性检查(2026-11-20)。

回归来源是用户实测的**逻辑冲突原文**:

    母亲住院那阵子,周航在镇上修电器,手里紧巴巴地垫出了三千元医药费,
    那会儿她咬着牙说年底一定还清。……如今还不到年底,她却只能先拿两千来抵一部分。

三处问题:①期限没到为什么要还;②"两千"不是"抵一部分"那样的量级表述;
③后文把这笔钱当租金来源,因果链悬空。根因不在句子,而在:写手输入里没有
"已确立事实"约束、评审 prompt 里没有自洽性判据、也没有确定性兜底。
"""

from __future__ import annotations

import inspect

import pytest

from app.services.established_facts import (
    EstablishedFacts,
    _to_int,
    check_chapter_against_facts,
    extract_established_facts,
)

_USER_FACT = ("母亲住院那阵子,周航在镇上修电器,手里紧巴巴地垫出了三千元医药费,"
              "那会儿她咬着牙说年底一定还清。")
_USER_CHAPTER = ("如今还不到年底,她却只能先拿两千来抵一部分,"
                 "转身又把这两千当作下个月的租金交给了房东。")


@pytest.mark.parametrize("token,expected", [
    ("三千", 3000), ("两千", 2000), ("一万", 10000), ("五百", 500),
    ("12", 12), ("二百五", 205), ("一千二百三十四万", 12340000),
])
def test_chinese_number_parsing(token, expected):
    assert _to_int(token) == expected


def test_extracts_obligation_sentences_with_amount_and_keyword():
    facts = extract_established_facts([_USER_FACT, "江风把灯影吹得摇晃。"])
    assert len(facts) == 1
    assert "三千元医药费" in facts[0]


def test_plain_narration_is_not_collected_as_fact():
    assert extract_established_facts(["三年后她回了镇上,两天没合眼。"]) == []


def test_block_renders_constraint_and_empty_when_no_facts():
    assert EstablishedFacts([]).block() == ""
    block = EstablishedFacts(["她欠他三千元,答应年底还清。"]).block()
    assert "已确立事实" in block and "不得与之矛盾" in block


def test_user_reported_sentence_is_flagged_before_deadline():
    """用户实测的那段必须被检出:未到期限就处置该笔钱。"""
    facts = extract_established_facts([_USER_FACT])
    findings = check_chapter_against_facts(
        _USER_CHAPTER, facts, chapter_time_hint="还不到年底")
    kinds = {f["kind"] for f in findings}
    assert "obligation_before_deadline" in kinds
    assert any(f["severity"] == "warning" for f in findings
               if f["kind"] == "obligation_before_deadline")


def test_amount_mismatch_reports_both_sides():
    facts = extract_established_facts([_USER_FACT])
    findings = [f for f in check_chapter_against_facts(_USER_CHAPTER, facts, check_amounts=True)
                if f["kind"] == "fact_amount_mismatch"]
    assert findings
    assert findings[0]["established_amounts"] == [3000]
    assert 2000 in findings[0]["chapter_amounts"]


def test_on_time_repayment_is_not_flagged():
    """按时履约不是矛盾 —— 这里报出来就是纯噪音。"""
    facts = extract_established_facts([_USER_FACT])
    assert check_chapter_against_facts(
        "年底到了,她把三千元一次还清。", facts, chapter_time_hint="年底到了") == []


def test_partial_repayment_on_same_debt_is_info_not_warning():
    """指向**同一笔账**的部分还款:报 info(不阻断),不升级成 warning。"""
    facts = extract_established_facts([_USER_FACT])
    findings = [f for f in check_chapter_against_facts(
        "她先还了那笔垫款两千元,剩下的年底给。", facts,
        chapter_time_hint="现在是夏天", check_amounts=True)
        if f["kind"] == "fact_amount_mismatch"]
    assert findings and all(f["severity"] == "info" for f in findings)


def test_amount_check_is_off_by_default():
    """真实 20 章实测:金额比较 18 报 18 误,所以默认关闭(见函数 docstring)。"""
    facts = extract_established_facts([_USER_FACT])
    assert [f for f in check_chapter_against_facts(_USER_CHAPTER, facts)
            if f["kind"] == "fact_amount_mismatch"] == []
    # 期限类不受影响,仍然会报
    assert any(f["kind"] == "obligation_before_deadline"
               for f in check_chapter_against_facts(_USER_CHAPTER, facts,
                                                    chapter_time_hint="还不到年底"))


def test_amounts_of_different_purposes_are_not_compared():
    """不同用途的钱不可比 —— 真实跑批里"八百块铺租"曾被误报成与欠款冲突。

    `verify-consistency-3` 第 2 章原句:"初五交八百块铺租的日子还摆在眼前,
    月末补齐周航那一千块的账也压在心底"。八百是铺租、一千才是那笔账,
    不出现指代(那笔/这笔/垫款/欠款)就不该把两者放在一起比。
    """
    facts = extract_established_facts([_USER_FACT])
    assert check_chapter_against_facts(
        "初五要交八百块铺租,她手里只剩一百二十块。", facts) == []


def test_plain_text_without_money_is_never_flagged():
    facts = extract_established_facts([_USER_FACT])
    assert check_chapter_against_facts("她一个人走了很久,风吹得屋檐响。", facts) == []


def test_small_numbers_like_yiding_are_not_amounts():
    """"一定/一阵/一起"里的数字不是金额。"""
    facts = extract_established_facts([_USER_FACT])
    findings = [f for f in check_chapter_against_facts(_USER_CHAPTER, facts, check_amounts=True)
                if f["kind"] == "fact_amount_mismatch"]
    assert findings and 1 not in findings[0]["chapter_amounts"]


# --- 接线护栏:三层必须都在生产链上 ----------------------------------------

def test_evaluator_prompt_carries_self_consistency_criteria():
    from app.services import chapter_evaluator

    prompt = chapter_evaluator.EVALUATION_SYSTEM_PROMPT
    for tag in ("continuity_fact_violation", "causal_chain_violation",
                "dangling_setup_violation"):
        assert tag in prompt, f"评审 prompt 缺少判据 {tag}"


def test_evaluator_injects_established_facts_into_user_prompt():
    from app.services import chapter_evaluator

    src = inspect.getsource(chapter_evaluator._build_user_prompt)
    assert "established_facts" in src
    assert "已确立事实" in src


def test_scene_writer_receives_established_facts_block():
    """写手侧也要拿到同一份事实清单,否则它只能现编。"""
    from app.services import scene_orchestrator

    src = inspect.getsource(scene_orchestrator)
    assert "build_established_facts" in src
    assert "fact_block" in src


def test_background_task_runs_deterministic_fact_check():
    from app.tasks import generation_tasks

    src = inspect.getsource(generation_tasks)
    assert "check_chapter_against_facts" in src


def test_new_causal_tags_are_hard_gated_when_located():
    from app.services.chapter_acceptance import _CAUSAL_TYPES

    assert "continuity_fact_violation" in _CAUSAL_TYPES
    assert "causal_chain_violation" in _CAUSAL_TYPES
    # 废话类不硬判:它是节奏问题,不是事实冲突
    assert "dangling_setup_violation" not in _CAUSAL_TYPES
