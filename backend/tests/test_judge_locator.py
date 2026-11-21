"""回归:评审给的原句要能被分级定位,而不是二值"逐字 / 不算"。

实测(2026-11-14):把"结果是否交付"交给另一个模型(deepseek-v4.1-flash)判定并要求逐字引用后,
它给出的引用**自己也不是逐字的**——把全角引号写成半角、把两句话拼在一起。二值判据下
结果是"交付率 0%"(严格)或"100%"(宽松),两种都没有分辨力。

所以改成三级可定位性:
- ``exact``      逐字包含;
- ``normalized`` 去空白 + 标点半角转全角后包含;
- ``partial``    最长公共子串 ≥ 12 字(评审确实指向了正文里的某一段);
- ``unlocated``  其余,视为编造。

判"已交付"必须至少 ``partial``;判"未交付"不需要引用(不交付是无法引用的)。
"""
from __future__ import annotations

from scripts.quality_eval.judge import _longest_common_substring, locate

TEXT = (
    "周芸把帆布包往肩上托了托。\n"
    "“这两千先拿着。”她说，“剩下的那一千,过两周我送到你店里。”\n"
    "周芸站在原地,看着渡船缓缓退开岸边,直到灯影散尽,她才转过身往回走。\n"
)


def test_exact_quote_is_exact():
    grade, _ = locate("“这两千先拿着。”她说", TEXT)
    assert grade == "exact"


def test_halfwidth_punctuation_is_normalized():
    """评审常把全角引号/逗号写成半角,这仍指向同一段原文。"""
    grade, _ = locate('"这两千先拿着。"她说,', TEXT)
    assert grade in ("normalized", "partial")


def test_merged_sentences_are_partial_but_located():
    """拼接两处会失去 exact,但仍能在正文里定位到足够长的公共片段。"""
    grade, common = locate("周芸站在原地,看着渡船缓缓退开岸边。她说,剩下的那一千过两周送到。", TEXT)
    assert grade == "partial"
    assert common >= 12


def test_fabricated_quote_is_unlocated():
    grade, common = locate("她数了三遍钞票,又把信封塞回棉袄的内袋里。", TEXT)
    assert grade == "unlocated"
    assert common < 12


def test_empty_quote_is_unlocated():
    assert locate("", TEXT)[0] == "unlocated"


def test_longest_common_substring_is_correct():
    assert _longest_common_substring("abcdef", "zzcdefzz") == 4
    assert _longest_common_substring("", "abc") == 0
