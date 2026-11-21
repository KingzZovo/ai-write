"""C2 / F1 stylestat: whole-book style statistics (pure functions + rendering).

Statistics belong in code (deterministic, zero LLM); these tests pin the
boundaries that matter -- stop-name filtering, substring dedup, the >=3-chapter
and min-length rules for repeated sentences, and the render budget contracts.
"""

from __future__ import annotations

from app.services.style_stat import (
    char_ngrams,
    compute_style_stats,
    ending_shape,
    extract_tic_counts,
    opening_time_rate,
    render_evaluator_stats_block,
    render_style_mirror_block,
    repeated_sentences,
    top_ngram_phrases,
)


def test_extract_tic_counts_patterns():
    text = (
        "他不是不想说，而是不能说。"     # corrective
        "几息之间，剑光已至。"           # time quantifier
        "那刀光如同闪电。"               # simile marker
        "她像受惊的兔子一样跳开。"        # simile-like
        "房间沉默了片刻。"              # silence beat
    )
    c = extract_tic_counts(text)
    assert c["corrective_not_but"] == 1
    assert c["time_quantifier"] == 1
    assert c["simile_marker"] >= 1   # "如同"
    assert c["simile_like"] == 1     # "像...一样"
    assert c["silence_beat"] == 1


def test_extract_tic_counts_empty():
    c = extract_tic_counts("")
    assert all(v == 0 for v in c.values())


def test_char_ngrams_han_only_no_cross_punct():
    grams = char_ngrams("你好，世界", 3)
    # "你好"(2) and "世界"(2) are separate runs, neither >= 3 chars
    assert grams == []
    grams4 = char_ngrams("江南春暖花开", 3)
    assert "江南春" in grams4 and "南春暖" in grams4
    # punctuation breaks runs
    assert char_ngrams("一二三。四五六", 4) == []


def test_top_ngram_stopnames_drops_grams_containing_name():
    # "林惊蛰走" repeated; name "林惊蛰" must drop any gram containing it.
    texts = ["林惊蛰走进房间。" * 10]
    phrases = top_ngram_phrases(texts, {"林惊蛰"}, threshold=5)
    for p in phrases:
        assert "林惊蛰" not in p["phrase"]


def test_top_ngram_threshold_filters():
    texts = ["甲乙丙丁戊。" * 3]   # each 5-run gram appears 3x
    # threshold 5 -> nothing survives; threshold 2 -> something does
    assert top_ngram_phrases(texts, set(), threshold=5) == []
    assert top_ngram_phrases(texts, set(), threshold=2)


def test_top_ngram_substring_dedup_keeps_longer():
    # "风萧萧兮" appears 10x; its substring "风萧萧" appears the same 10x ->
    # keep the longer phrase, drop the short one.
    texts = ["风萧萧兮。" * 10]
    phrases = top_ngram_phrases(texts, set(), threshold=5)
    out = {p["phrase"] for p in phrases}
    assert "风萧萧兮" in out
    assert "风萧萧" not in out


def test_repeated_sentences_needs_three_distinct_chapters():
    sent = "这是一句足够长的重复句子用于测试"  # len >= 10
    # appears in chapters 1 and 2 only -> below the 3-chapter floor
    two = [(1, sent + "。"), (2, sent + "。")]
    assert repeated_sentences(two) == []
    three = two + [(3, sent + "。")]
    hits = repeated_sentences(three)
    assert hits and hits[0]["sentence"] == sent
    assert hits[0]["chapter_count"] == 3


def test_repeated_sentences_ignores_short_sentences():
    short = "他点头。"  # len < 10
    chs = [(1, short), (2, short), (3, short), (4, short)]
    assert repeated_sentences(chs) == []


def test_repeated_sentences_same_chapter_counts_once():
    sent = "这是一句足够长的重复句子用于测试"
    # 3 copies all in ONE chapter -> only 1 distinct chapter -> no hit
    one = [(1, "。".join([sent, sent, sent]) + "。")]
    assert repeated_sentences(one) == []


def test_ending_shape_short_end_rate():
    chapters = [(1, "很长的开头叙述铺垫情节推进。他走了。"), (2, "另一段长长的叙述内容在这里。她笑了。")]
    shape = ending_shape(chapters)
    assert shape["chapters_counted"] == 2
    assert shape["short_ending_rate"] == 1.0  # both end on short sentences


def test_opening_time_rate():
    chapters = [(1, "清晨，他醒来。"), (2, "夜色中她潜行。"), (3, "他握紧了拳头。")]
    rate = opening_time_rate(chapters)
    assert rate["opening_time_rate"] == round(2 / 3, 3)


def test_compute_style_stats_empty():
    assert compute_style_stats([], set()) == {"chapter_count": 0}


def test_compute_style_stats_recent_window():
    # 30 chapters; recent_window=20 should restrict n-gram corpus to last 20.
    chapters = [(i, f"第{i}章内容。江湖夜雨十年灯。" * 3) for i in range(1, 31)]
    stats = compute_style_stats(chapters, set(), recent_window=20)
    assert stats["chapter_count"] == 30
    assert stats["recent_window"] == 20
    assert stats["ngram_threshold"] == max(8, 20 // 2)


# --- render budget contracts ------------------------------------------------


def _rich_stats() -> dict:
    chapters = [
        (i, "他不是A而是B。" + "江湖夜雨十年灯。" * 4 + "他走了。")
        for i in range(1, 25)
    ]
    return compute_style_stats(chapters, set())


def test_render_mirror_budget_contract():
    block = render_style_mirror_block(_rich_stats(), max_chars=800)
    assert 0 < len(block) <= 800
    assert "文风镜像" in block


def test_render_evaluator_budget_contract():
    block = render_evaluator_stats_block(_rich_stats(), max_chars=600)
    assert 0 < len(block) <= 600
    assert "全书文风统计" in block


def test_render_empty_stats_returns_empty():
    assert render_style_mirror_block({"chapter_count": 0}) == ""
    assert render_evaluator_stats_block({}) == ""
    assert render_style_mirror_block({}) == ""


# --- injection tripwires (mirror test_cognition_injection.py) ---------------


def test_context_pack_injects_style_mirror():
    """ContextPack.to_system_prompt must surface style_tic_mirror when present
    and omit the section when empty."""
    from app.services.context_pack import ContextPack

    pack = ContextPack()
    pack.style_tic_mirror = "【文风镜像（高频口头禅，主动压低）】\n- 高频短语：江湖夜雨(12)"
    out = pack.to_system_prompt()
    assert "文风镜像" in out
    assert "江湖夜雨" in out

    empty = ContextPack()
    assert "文风镜像" not in empty.to_system_prompt()


def test_evaluator_user_prompt_injects_style_stats():
    """_build_user_prompt must surface style_stats_text when present, omit when empty."""
    from app.services.chapter_evaluator import _build_user_prompt

    with_stats = _build_user_prompt(
        chapter_text="正文",
        chapter_outline={},
        previous_summary="",
        style_profile="",
        active_foreshadows=None,
        style_stats_text="## 全书文风统计（参考数字，是否构成问题由你裁定）\n- 句式章均频率：simile_marker=2.0/章",
    )
    assert "全书文风统计" in with_stats

    without = _build_user_prompt(
        chapter_text="正文",
        chapter_outline={},
        previous_summary="",
        style_profile="",
        active_foreshadows=None,
    )
    assert "全书文风统计" not in without
