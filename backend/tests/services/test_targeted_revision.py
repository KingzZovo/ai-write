"""Task 16 (Q2) — targeted span revision driven by evaluator `quote` field.

QMAI rewriteTarget: the evaluator emits a verbatim 10-40 char `quote` per
issue; auto-revise locates the containing paragraph (±1 paragraph), rewrites
only that span via one LLM call per merged span, and splices the result back.
Issues whose quote is missing or cannot be located fall back to the existing
full-chapter regeneration path.
"""
from __future__ import annotations

import asyncio


# ---------------------------------------------------------------------------
# Pure helpers: locate / splice / merge
# ---------------------------------------------------------------------------

def test_locate_span_by_quote():
    from app.services.auto_revise import locate_revision_span

    text = "第一段。\n\n第二段有一个错误描写在这里。\n\n第三段。\n\n第四段。"
    span = locate_revision_span(text, "错误描写在这里")
    assert span is not None
    start, end = span
    assert "第一段" in text[start:end]
    assert "第三段" in text[start:end]
    assert "第四段" not in text[start:end]


def test_locate_span_quote_missing_returns_none():
    from app.services.auto_revise import locate_revision_span

    assert locate_revision_span("正文内容", "不存在的片段") is None
    assert locate_revision_span("正文内容", "") is None
    assert locate_revision_span("", "片段") is None


def test_locate_span_first_paragraph_not_widened_below_zero():
    from app.services.auto_revise import locate_revision_span

    text = "开头段有问题描写。\n\n第二段。\n\n第三段。"
    span = locate_revision_span(text, "开头段有问题描写")
    assert span is not None
    start, end = span
    assert start == 0
    assert "第二段" in text[start:end]
    assert "第三段" not in text[start:end]


def test_splice_revised_span():
    from app.services.auto_revise import splice_revision

    text = "AAA\n\nBBB\n\nCCC"
    out = splice_revision(text, (5, 8), "DDD")
    assert out == "AAA\n\nDDD\n\nCCC"


def test_merge_overlapping_spans():
    from app.services.auto_revise import merge_spans

    assert merge_spans([(0, 10), (5, 20), (30, 40)]) == [(0, 20), (30, 40)]
    assert merge_spans([]) == []
    # Adjacent spans merge too (end == start).
    assert merge_spans([(0, 10), (10, 15)]) == [(0, 15)]
    # Unsorted input is sorted first.
    assert merge_spans([(30, 40), (0, 10)]) == [(0, 10), (30, 40)]


# ---------------------------------------------------------------------------
# revise_spans behavior with a fake llm_call
# ---------------------------------------------------------------------------

_TEXT = "第一段开场。\n\n第二段有一个错误描写在这里需要修。\n\n第三段过渡。\n\n第四段收尾完全不要动。"


def test_revise_spans_replaces_located_span():
    from app.services.auto_revise import revise_spans

    issues = [{
        "dimension": "plot_coherence",
        "description": "空间跳跃",
        "suggestion": "补一句移动过程",
        "quote": "错误描写在这里需要修",
    }]

    async def fake_llm(prompt: str) -> str:
        assert "错误描写在这里需要修" in prompt
        return "改写后的区间文本，问题已经修复。"

    result = asyncio.run(revise_spans(_TEXT, issues, llm_call=fake_llm))
    assert result.spans_revised == 1
    assert result.unlocatable_issues == []
    assert "改写后的区间文本" in result.text
    assert "错误描写在这里需要修" not in result.text
    # The span is hit paragraph ±1 — the untouched fourth paragraph survives.
    assert result.text.endswith("第四段收尾完全不要动。")


def test_revise_spans_all_unlocatable_signals_fallback():
    from app.services.auto_revise import revise_spans

    issues = [
        {"description": "无 quote 的旧式 issue", "suggestion": ""},
        {"description": "quote 定位失败", "quote": "原文里根本不存在的句子"},
    ]
    calls: list[str] = []

    async def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return "不应该被调用"

    result = asyncio.run(revise_spans(_TEXT, issues, llm_call=fake_llm))
    assert result.spans_revised == 0
    assert result.text == _TEXT
    assert len(result.unlocatable_issues) == 2
    assert calls == []


def test_revise_spans_merges_same_region_into_one_call():
    from app.services.auto_revise import revise_spans

    issues = [
        {"description": "问题甲", "quote": "第二段有一个错误描写"},
        {"description": "问题乙", "quote": "错误描写在这里需要修"},
    ]
    calls: list[str] = []

    async def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return "合并区间一次性改写结果。"

    result = asyncio.run(revise_spans(_TEXT, issues, llm_call=fake_llm))
    assert result.spans_revised == 1
    assert len(calls) == 1
    # Both issue descriptions are surfaced in the single rewrite prompt.
    assert "问题甲" in calls[0]
    assert "问题乙" in calls[0]


def test_revise_spans_rejects_degenerate_rewrite():
    from app.services.auto_revise import revise_spans

    issues = [{"description": "问题", "quote": "错误描写在这里需要修"}]

    async def empty_llm(prompt: str) -> str:
        return "   "

    result = asyncio.run(revise_spans(_TEXT, issues, llm_call=empty_llm))
    assert result.spans_revised == 0
    assert result.text == _TEXT


def test_targeted_revision_enabled_env_switch(monkeypatch):
    from app.services.auto_revise import targeted_revision_enabled

    monkeypatch.delenv("TARGETED_REVISION_ENABLED", raising=False)
    assert targeted_revision_enabled() is True
    monkeypatch.setenv("TARGETED_REVISION_ENABLED", "0")
    assert targeted_revision_enabled() is False
    monkeypatch.setenv("TARGETED_REVISION_ENABLED", "1")
    assert targeted_revision_enabled() is True


# ---------------------------------------------------------------------------
# Evaluator quote field: prompt contract + parse passthrough
# ---------------------------------------------------------------------------

def test_evaluation_prompt_requests_quote_field():
    from app.services.chapter_evaluator import EVALUATION_SYSTEM_PROMPT

    assert '"quote"' in EVALUATION_SYSTEM_PROMPT
    assert "10-40" in EVALUATION_SYSTEM_PROMPT
    # The verbatim-extraction requirement must be stated.
    assert "逐字" in EVALUATION_SYSTEM_PROMPT


def test_parse_evaluation_response_preserves_quote():
    import json

    from app.services.chapter_evaluator import _parse_evaluation_response

    raw = json.dumps({
        "plot_coherence": {
            "score": 6,
            "issues": [{
                "paragraph": 2,
                "description": "空间跳跃",
                "suggestion": "补移动过程",
                "violation_type": "space_rule_violation",
                "severity": "high",
                "quote": "他忽然出现在城东的仓库里",
            }],
        },
        "character_consistency": {"score": 8, "issues": []},
        "style_adherence": {"score": 8, "issues": []},
        "narrative_pacing": {"score": 8, "issues": []},
        "foreshadow_handling": {"score": 8, "issues": []},
        "contract_violations": [{
            "violation_type": "information_rule_violation",
            "paragraph": 5,
            "description": "无来源情报",
            "quote": "她早就知道了对方的底牌",
        }],
    }, ensure_ascii=False)

    result = _parse_evaluation_response(raw)
    quotes = [i.get("quote") for i in result.issues]
    assert "他忽然出现在城东的仓库里" in quotes
    assert "她早就知道了对方的底牌" in quotes


# ---------------------------------------------------------------------------
# Caller wiring tripwires (same approach as test_chapter_target_words.py)
# ---------------------------------------------------------------------------

def test_sse_revise_loop_wired_to_targeted_revision():
    import inspect
    import re

    from app.api import generate

    src = re.sub(r"\s+", "", inspect.getsource(generate))
    assert "revise_spans(" in src
    assert "targeted_revision_enabled()" in src


def test_celery_revise_loop_wired_to_targeted_revision():
    import inspect
    import re

    from app.tasks import knowledge_tasks

    src = re.sub(r"\s+", "", inspect.getsource(knowledge_tasks))
    assert "revise_spans(" in src
    assert "targeted_revision_enabled()" in src
