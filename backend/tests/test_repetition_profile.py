"""章内句级重复度证据(2026-11-28 全书质量检查)。

背景:段落级重复由 `prose_sanitizer.collapse_duplicate_paragraphs` 在落库前折叠,
但**句级**模板循环它抓不到(实测 卷5-3「掌根裂痕烫了#下」×33、卷3-52
「他记得这些」×28、卷3-16 纹路枚举 ×12),评审也没被喂这个信号,于是整批进
needs_review。契约明确"重复要结合语境判断、不按词频清零",所以这里只做**计数**,
判断仍归模型 —— 本文件锁死"数得对"与"不越界下判断"。
"""
from __future__ import annotations

from app.services.repetition_profile import (
    chapter_repetition_profile,
    render_repetition_block,
)


def test_counts_a_repeated_sentence_template():
    text = "他记得这些。" * 8 + "他站起来,走到门口。"
    profile = chapter_repetition_profile(text)
    assert profile["max_repeat"] == 8
    assert profile["templates"][0] == (8, "他记得这些")


def test_masks_numbers_so_numbered_variants_share_one_template():
    text = "".join(f"他画第{n}道线。" for n in "一二三四五")
    profile = chapter_repetition_profile(text)
    assert profile["max_repeat"] == 5
    assert profile["templates"][0][1] == "他画第#道线"


def test_short_clauses_are_not_counted():
    """「他停住。」这类短句天然会重复,不能变成证据。"""
    profile = chapter_repetition_profile("他停住。他停住。他停住。他停住。他停住。")
    assert profile["templates"] == []


def test_detects_long_enumerated_run():
    text = "、".join(f"第{n}道纹路" for n in "一二三四五六七八九十") + "都留在阴影里。"
    profile = chapter_repetition_profile(text)
    assert profile["enum_run"] >= 6
    assert profile["enum_example"] == "第#道纹路"


def test_clean_prose_produces_no_evidence_block():
    text = (
        "傍晚的垃圾站没什么人。他绕过铁皮棚,停在一辆平板车后面。\n\n"
        "车斗里压着半箱旧报纸,纸角被雨泡过,发硬。他抽出一张,折成四方,塞进内兜。\n\n"
        "远处有人喊了一声,声音在巷子里散开,听不出方向。他没有回头。"
    )
    assert render_repetition_block(chapter_repetition_profile(text)) == ""


def test_block_keeps_the_contract_open_and_asks_the_model_to_judge():
    """证据块不得变成硬禁令:必须显式把判断权交回模型并允许有意复沓。"""
    block = render_repetition_block(chapter_repetition_profile("他记得这些。" * 8))
    assert "他记得这些" in block
    assert "结合语境判断" in block
    assert "有意复沓" in block


def test_profile_does_not_emit_a_verdict():
    profile = chapter_repetition_profile("他记得这些。" * 8)
    text = render_repetition_block(profile)
    # 只禁"下判决",不禁"说明这不是评分"这类澄清语句。
    for forbidden in ("必须重写", "禁止重复", "不合格", "应判为"):
        assert forbidden not in text