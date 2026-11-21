"""Structure-aware chapter chunking (纯函数,无 DB / 无 Qdrant / 无模型调用).

Covers the recall-quality contract of ``chunk_chapter_text``:

- a spoken exchange (consecutive quote-opening paragraphs) is never cut while
  it fits in ``dialogue_char_cap``; past the cap it is broken only *between*
  turns;
- a blank line or a scene/time marker starts a new chunk even below
  ``CHUNK_TARGET_CHARS``;
- over-long paragraphs still break at sentence enders, single over-long
  sentences hard-split;
- no chunk exceeds ``CHUNK_MAX_CHARS * 1.2``;
- empty / degenerate input never raises;
- chunking is deterministic and point ids keep the md5 convention.
"""
from __future__ import annotations

import hashlib

from app.services.chapter_chunker import (
    CHUNK_MAX_CHARS,
    CHUNK_TARGET_CHARS,
    chunk_chapter_text,
    chunk_point_id,
    dialogue_char_cap,
)

_SENT = "他说这句话的时候声音很低,像是在压着什么。"
_CAP = dialogue_char_cap()  # CHUNK_MAX_CHARS * 1.2 == 960


def _body(approx: int) -> str:
    """Roughly ``approx`` chars of narrative prose (sentence terminated)."""
    return _SENT * max(1, round(approx / len(_SENT)))


def _turn(tag: str, approx: int) -> str:
    """One dialogue turn: quote-opening paragraph of roughly ``approx`` chars."""
    return f"“{tag}:" + _body(approx) + "”"


def _paragraphs(chunks: list[str]) -> list[str]:
    return [p for c in chunks for p in c.split("\n")]


class TestDialogueExchangeIntegrity:
    def test_eight_turns_split_between_turns_never_inside(self):
        """8 轮对白(总长 > 硬上限):只在轮次之间断,任何一轮都不被切开。"""
        turns = [_turn(f"第{i}轮", 130) for i in range(1, 9)]
        assert sum(len(t) for t in turns) > _CAP  # precondition: over hard cap
        chunks = chunk_chapter_text("\n".join(turns))

        assert len(chunks) > 1
        # Every paragraph is one COMPLETE turn — no turn was sliced in half.
        assert _paragraphs(chunks) == turns
        for chunk in chunks:
            assert chunk.split("\n")[0] in turns
            assert len(chunk) <= _CAP
        # Boundaries fall on turn boundaries and preserve book order.
        assert [p for p in _paragraphs(chunks)] == turns
        # Each chunk holds whole, consecutive turns.
        pos = 0
        for chunk in chunks:
            n = len(chunk.split("\n"))
            assert chunk.split("\n") == turns[pos:pos + n]
            pos += n
        assert pos == len(turns)

    def test_three_turns_over_target_stay_in_one_chunk(self):
        """连续 3 个引号段落总长 ≤ 硬上限:即使超过 target 也不拆开。"""
        turns = [_turn(f"第{i}轮", 240) for i in range(1, 4)]
        joined = "\n".join(turns)
        assert CHUNK_TARGET_CHARS < len(joined) <= _CAP  # precondition
        chunks = chunk_chapter_text(joined)

        assert chunks == [joined]
        assert len(chunks[0]) > CHUNK_TARGET_CHARS

    def test_single_turn_never_merged_past_cap(self):
        """对白单元可以超过 target,但绝不超过 CHUNK_MAX_CHARS * 1.2。"""
        turns = [_turn(f"第{i}轮", 200) for i in range(1, 13)]
        chunks = chunk_chapter_text("\n".join(turns))
        for chunk in chunks:
            assert len(chunk) <= _CAP
        assert _paragraphs(chunks) == turns

    def test_dialogue_exchange_breaks_at_scene_gap(self):
        """**双**空行(作者标出的分场)结束一个对白往来:两个往来不共处一个 chunk。"""
        first = [_turn("甲", 120), _turn("乙", 120)]
        second = [_turn("丙", 120), _turn("丁", 120)]
        text = "\n".join(first) + "\n\n\n" + "\n".join(second)
        chunks = chunk_chapter_text(text)

        assert len(chunks) == 2
        assert chunks[0].split("\n") == first
        assert chunks[1].split("\n") == second

    def test_overlong_dialogue_paragraph_falls_back_to_sentence_split(self):
        """引号段落本身超长:回到句边界切,且不超过 max_chars。"""
        para = "“" + _body(CHUNK_MAX_CHARS * 3) + "”"
        chunks = chunk_chapter_text(para)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= CHUNK_MAX_CHARS
            assert chunk[-1] in "。!?…!?”"


class TestSceneBreakPriority:
    def test_scene_marker_starts_new_chunk_below_target(self):
        head = _body(60)
        tail = "次日清晨," + _body(80)
        assert len(head) + len(tail) < CHUNK_TARGET_CHARS  # precondition
        chunks = chunk_chapter_text(head + "\n" + tail)

        assert chunks == [head, tail]

    def test_double_blank_line_starts_new_chunk_below_target(self):
        """两个连续空行 = 分场:即使远没到 500 字也断开。"""
        head = _body(60)
        tail = _body(80)
        assert len(head) + len(tail) < CHUNK_TARGET_CHARS
        chunks = chunk_chapter_text(head + "\n\n\n" + tail)

        assert chunks == [head, tail]

    def test_single_blank_line_is_a_paragraph_gap_not_a_scene_break(self):
        """单个空行只是段落分隔(中文网文常态),不能当分场。

        回归(2026-11-17):把单空行当分场后,100 章真实语料上片段数从 230 涨到
        1109、对白被切断从 4 处涨到 37 处 —— 每段都被拆成 ~90 字的碎片。
        """
        head = _body(60)
        tail = _body(80)
        chunks = chunk_chapter_text(head + "\n\n" + tail)
        assert chunks == [head + "\n" + tail]

    def test_all_marker_examples_break(self):
        head = _body(40)
        for marker in ("次日", "第二天", "与此同时", "另一头", "画面一转"):
            tail = marker + "风从巷口灌进来。"
            chunks = chunk_chapter_text(head + "\n" + tail)
            assert chunks == [head, tail], marker

    def test_dash_paragraph_opener_is_not_a_scene_marker(self):
        """`——` 在中文里首先是破折号/打断语,不能当分场标记(否则对白会被切碎)。"""
        head = _body(40)
        tail = "——风从巷口灌进来。"
        assert chunk_chapter_text(head + "\n" + tail) == [head + "\n" + tail]

    def test_no_scene_break_keeps_short_paragraphs_together(self):
        paras = [_body(60) for _ in range(3)]
        text = "\n".join(paras)
        assert len(text) < CHUNK_TARGET_CHARS
        assert chunk_chapter_text(text) == [text]

    def test_multiple_blank_lines_are_one_gap(self):
        head = _body(50)
        tail = _body(50)
        chunks = chunk_chapter_text(head + "\n\n\n   \n" + tail)
        assert chunks == [head, tail]


class TestSentenceBoundaries:
    def test_overlong_paragraph_split_at_sentence_end(self):
        para = _body(CHUNK_MAX_CHARS * 2)
        chunks = chunk_chapter_text(para)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= CHUNK_MAX_CHARS
            assert chunk[-1] in "。!?…!?"

    def test_single_overlong_sentence_hard_split(self):
        chunks = chunk_chapter_text("字" * 2000)
        assert [len(c) for c in chunks] == [800, 800, 400]
        for chunk in chunks:
            assert len(chunk) <= CHUNK_MAX_CHARS

    def test_lossless_paragraph_stream(self):
        text = "\n".join(
            [_turn("甲", 200), _body(300), _turn("乙", 400), "次日" + _body(200)]
        )
        chunks = chunk_chapter_text(text)
        assert "".join(_paragraphs(chunks)) == "".join(text.split("\n"))


class TestBoundsAndEdgeCases:
    def test_mixed_chapter_respects_hard_cap(self):
        text = "\n".join(
            [
                _body(900),  # over-long narrative paragraph
                _turn("甲", 230),
                _turn("乙", 230),
                _turn("丙", 230),
                "",
                "与此同时," + _body(700),
                _turn("丁", 150),
                "字" * 1200,  # single over-long sentence
            ]
        )
        chunks = chunk_chapter_text(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert 0 < len(chunk) <= _CAP

    def test_empty_and_degenerate_inputs(self):
        assert chunk_chapter_text("") == []
        assert chunk_chapter_text("   ") == []
        assert chunk_chapter_text("\n\n\n") == []
        assert chunk_chapter_text(" \n \n\t\n ") == []
        assert chunk_chapter_text("一") == ["一"]
        assert chunk_chapter_text("夜里下了雨。\n林岚推门进来。") == [
            "夜里下了雨。\n林岚推门进来。"
        ]

    def test_single_paragraph_is_single_chunk(self):
        para = _body(200)
        assert chunk_chapter_text(para) == [para]

    def test_deterministic(self):
        text = "\n".join(
            [_turn("甲", 300), _turn("乙", 300), "", _body(400), "次日" + _body(200)]
        )
        first = chunk_chapter_text(text)
        second = chunk_chapter_text(text)
        assert first == second
        # Fresh call on equal-but-distinct input is identical too.
        assert chunk_chapter_text("\n".join(text.split("\n"))) == first

    def test_custom_max_chars_scales_dialogue_cap(self):
        turns = [_turn(f"第{i}轮", 100) for i in range(1, 4)]
        chunks = chunk_chapter_text("\n".join(turns), max_chars=400)
        assert all(len(c) <= dialogue_char_cap(400) for c in chunks)

    def test_point_id_convention_unchanged(self):
        vid = "0b6b9b0e-0000-0000-0000-000000000000"
        expected = int(
            hashlib.md5(f"{vid}_4_1".encode()).hexdigest()[:16], 16
        )
        assert chunk_point_id(vid, 4, 1) == expected