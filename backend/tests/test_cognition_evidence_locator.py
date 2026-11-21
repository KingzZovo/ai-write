"""回归:认知提取的"逐字依据"必须可用,且不合格时要能定向修复。

实测(2026-11-14 真实链连续章):gemini-3.1-pro 在 temperature 0.2 下仍会把
evidence 写成自己的话。5 条里 2 条能在正文里找到,3 条找不到(改写/拼接)。
后果是**阻断**:`cognition_extraction` 连续失败后,该章没有 completed 的认知记录,
下一章直接被 `character_cognition_not_ready` 拦住,整个连续写作停住。

这里固定两件事:
1. 找不到的依据会被指出(异常带上 ``offending_evidence``),让调用方的修复提示能点名;
2. 逐字存在的依据照常通过。
"""
from __future__ import annotations

import pytest

from app.services.chapter_entity_extraction import EntityExtractionError
from app.services.character_cognition import validate_changes

SOURCE = (
    "“姐,你来了。”周航把工具包放在凳子上。\n"
    "周芸从布包里掏出两千块钱,递了过去。\n"
    "周航说:“妈住院那回我垫了三千,你还差一千。”\n"
)


def test_verbatim_evidence_passes():
    changes = [{"character": "周航", "learns": "姐姐只凑到两千", "evidence": "周芸从布包里掏出两千块钱,递了过去。"}]
    assert validate_changes(changes, SOURCE) == changes


def test_rewritten_evidence_is_rejected_and_named():
    """改写过的"依据"必须被拒,并且把这一条原文带出来供修复提示使用。"""
    rewritten = "周芸从布包里掏出她攒下的全部积蓄,递给弟弟。"
    changes = [
        {"character": "周航", "learns": "姐姐只凑到两千", "evidence": "周芸从布包里掏出两千块钱,递了过去。"},
        {"character": "周航", "learns": "姐姐还差一千", "evidence": rewritten},
    ]
    with pytest.raises(EntityExtractionError) as excinfo:
        validate_changes(changes, SOURCE)
    assert getattr(excinfo.value, "offending_evidence", None) == [rewritten]


def test_evidence_with_ellipsis_is_rejected():
    """拼接两处、加省略号都算改写 —— 逐字片段必须连续存在。"""
    changes = [{"character": "周航", "learns": "缺一千", "evidence": "周航说:“妈住院那回我垫了三千......你还差一千。”"}]
    with pytest.raises(EntityExtractionError):
        validate_changes(changes, SOURCE)


def test_evidence_spanning_a_line_break_is_rejected_only_when_not_contiguous():
    """跨段引用如果原文里确实连续(含换行原样复制)则通过,否则拒绝。"""
    contiguous = {"character": "周航", "learns": "姐姐来了", "evidence": "“姐,你来了。”周航把工具包放在凳子上。"}
    assert validate_changes([contiguous], SOURCE) == [contiguous]
    joined = {"character": "周航", "learns": "姐姐来了", "evidence": "“姐,你来了。”周航把工具包放在凳子上。周芸从布包里掏出两千块钱,递了过去。"}
    with pytest.raises(EntityExtractionError):
        validate_changes([joined], SOURCE)


def test_empty_change_list_is_valid():
    """没有变化是合法结果,不能被当成失败。"""
    assert validate_changes([], SOURCE) == []


def test_halfwidth_punctuation_form_still_counts_as_verbatim():
    """全角/半角标点形式的差异不算改写。

    实测(2026-11-14 连续 24 章):第 6 章认知提取连续 3 次报"逐字定位"失败,
    第 7 章起 `character_cognition_not_ready`,整条链停摆;而模型给出的依据
    只是把全角引号写成了半角。这条偏差必须容忍,但不能放宽到省略号/拼接。
    """
    halfwidth = {"character": "周航", "learns": "姐姐来了",
                 "evidence": "\"姐,你来了。\"周航把工具包放在凳子上"}
    assert validate_changes([halfwidth], SOURCE) == [halfwidth]


def test_folding_does_not_accept_ellipsis_or_stitching():
    """归一化只统一标点形式:删掉省略号后能拼上的仍然必须拒绝。"""
    from app.services.chapter_entity_extraction import evidence_locatable
    assert not evidence_locatable("“姐,你来了。”……周航把工具包放在凳子上。", SOURCE)
    assert not evidence_locatable("工具包放在凳子上。周芸从布包里掏出两千块钱,递了过去。", SOURCE)
