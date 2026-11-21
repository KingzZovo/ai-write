"""Regression: a chapter that ends mid-sentence must not be marked completed.

Live failure (2026-06-29 全流程测试): the last scene's scene_writer call hit its
max_tokens ceiling; the OpenAI-compat stream ended mid-sentence; model_router's
generate_stream never inspects finish_reason, so the orchestrator appended the
partial scene and moved on. The whole chapter still cleared the aggregate
length floor (only 1 of N scenes was cut), so it was saved with status
"completed" while literally ending on a dangling half-sentence, e.g.

    顾远用那把粗砺的重型潜水凿，狠狠砸向值班室正中央那块早已

`looks_truncated` detects a chapter body whose final non-empty line does not end
on a sentence-terminal punctuation mark (。！？…" 」』 etc.), so the caller can
flag/withhold the completed status instead of persisting a dangling chapter.
"""
from __future__ import annotations

import pytest

from app.services.chapter_quality_gate import looks_truncated


@pytest.mark.parametrize("text", [
    "顾远用那把粗砺的重型潜水凿，狠狠砸向值班室正中央那块早已",   # dangles
    "他转过身，看向那片黑暗，缓缓开口说道",                       # dangles on 说道
    "第一段结束。\n\n第二段还没写完，突然断在这里",              # last line dangles
])
def test_flags_midsentence_ending(text):
    assert looks_truncated(text) is True


@pytest.mark.parametrize("text", [
    "他终于把门关上了。",                       # 。
    "“你到底想干什么？”顾远低声问。",           # 。 after quote
    "雨还在下。\n\n他没有回头，径直走进了夜色里。",  # multi-para, clean end
    "一切都结束了！",                           # ！
    "他喃喃自语：“原来如此……”",                 # ellipsis inside close-quote
    "他合上了那本书」",                          # CJK close bracket
])
def test_accepts_complete_ending(text):
    assert looks_truncated(text) is False


def test_empty_or_blank_is_not_truncated():
    # Empty text is a different failure (handled elsewhere); don't false-flag.
    assert looks_truncated("") is False
    assert looks_truncated("   \n  ") is False


def test_trailing_whitespace_and_fences_ignored():
    # Trailing blank lines / stray closing fence must not hide a clean ending.
    assert looks_truncated("他终于把门关上了。\n\n") is False
    assert looks_truncated("他终于把门关上了。\n```") is False
    # ...but a dangling body with trailing blanks is still truncated.
    assert looks_truncated("他狠狠砸向那块早已\n\n") is True
