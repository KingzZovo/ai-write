"""B2 hotfix: evaluator JSON parse resilience + parse-failure revision gate.

Production failure mode (found by the B1 end-to-end smoke run): the
evaluation LLM emits raw control characters inside JSON string values --
typically a literal newline inside an issue ``quote`` excerpt -- so
``json.loads`` (strict=True by default) raises
``Invalid control character at: ...``. ``evaluate()`` caught that and
returned an all-zero EvaluationResult with one synthetic quote-less issue,
``should_revise(0 < 8.2)`` then forced a revision round, and because the
synthetic issue carries no quote, targeted revision could not locate
anything and fell back to a full-chapter rewrite (~10 min wasted per round).

Two-layer fix under test:
1. ``_parse_evaluation_response`` tolerates control chars in string values
   (strict=False, plus a strip-and-retry for anything still unparseable).
2. ``EvaluationResult.parse_failed`` is set when parsing genuinely fails,
   and ``should_revise`` returns False for such untrusted results.
"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.auto_revise import should_revise
from app.services.chapter_evaluator import (
    ChapterEvaluator,
    EvaluationResult,
    _parse_evaluation_response,
)
from app.services.model_router import GenerationResult, TokenUsage


QUOTE_PLACEHOLDER = "__QUOTE_PLACEHOLDER__"


def _five_dim_payload_with_raw_quote(raw_quote: str) -> str:
    """Valid 5-dimension JSON, then splice RAW control chars into the quote.

    json.dumps would escape \\n / \\x0b, so we serialize with a placeholder
    and substitute afterwards -- reproducing exactly what the LLM emitted.
    """
    payload = json.dumps(
        {
            "plot_coherence": {
                "score": 6,
                "issues": [
                    {
                        "paragraph": 3,
                        "description": "空间跳跃缺少过程",
                        "suggestion": "补移动过程",
                        "violation_type": "space_rule_violation",
                        "severity": "high",
                        "quote": QUOTE_PLACEHOLDER,
                    }
                ],
            },
            "character_consistency": {"score": 7, "issues": []},
            "style_adherence": {"score": 8, "issues": []},
            "narrative_pacing": {"score": 7, "issues": []},
            "foreshadow_handling": {"score": 9, "issues": []},
        },
        ensure_ascii=False,
    )
    return payload.replace(QUOTE_PLACEHOLDER, raw_quote)


def test_parse_survives_control_chars_in_quote():
    """Raw \\n and \\x0b inside a quote value must not zero out the scores.

    Before the fix: json.loads(strict=True) raises JSONDecodeError.
    After: parses fine; control chars may be stripped or \\n kept -- we only
    require no crash, correct scores, and a non-empty surviving quote.
    """
    raw_quote = "他忽然出现在\n城东的仓库\x0b里面"
    raw = _five_dim_payload_with_raw_quote(raw_quote)

    result = _parse_evaluation_response(raw)

    assert result.parse_failed is False
    assert result.plot_coherence == pytest.approx(6.0)
    assert result.character_consistency == pytest.approx(7.0)
    assert result.style_adherence == pytest.approx(8.0)
    assert result.narrative_pacing == pytest.approx(7.0)
    assert result.foreshadow_handling == pytest.approx(9.0)
    assert result.overall == pytest.approx(7.4)

    quotes = [i.get("quote", "") for i in result.issues]
    surviving = [q for q in quotes if "他忽然出现在" in q]
    assert surviving, f"quote locator lost during tolerant parse: {quotes}"
    # The \x0b must be gone or harmless; \n may be kept or stripped.
    assert "城东的仓库" in surviving[0].replace("\x0b", "")


def test_parse_strips_markdown_fence():
    """```json fenced payload -> parses fine (existing defense kept intact)."""
    payload = _five_dim_payload_with_raw_quote("他忽然出现在城东的仓库里面")
    raw = f"```json\n{payload}\n```"

    result = _parse_evaluation_response(raw)

    assert result.parse_failed is False
    assert result.overall == pytest.approx(7.4)


def test_parse_extracts_json_from_surrounding_prose():
    """LLM chatter around the JSON object -> repair retry slices first '{'
    to last '}' and still parses (with raw control chars inside, too)."""
    payload = _five_dim_payload_with_raw_quote("他忽然出现在\n城东的仓库")
    raw = f"好的，以下是本章的评估结果：\n\n{payload}\n\n希望对修订有帮助。"

    result = _parse_evaluation_response(raw)

    assert result.parse_failed is False
    assert result.overall == pytest.approx(7.4)


def test_parse_strip_retry_handles_other_bare_control_chars():
    """Even \\x00-style control chars (rejected by some decoders) survive via
    the strip-and-retry layer without zeroing the evaluation."""
    raw = _five_dim_payload_with_raw_quote("证物\x01凭空\x1f出现")
    result = _parse_evaluation_response(raw)
    assert result.parse_failed is False
    assert result.overall == pytest.approx(7.4)


@pytest.mark.asyncio
async def test_parse_failed_flag_set_on_garbage():
    """Completely unparseable LLM output -> EvaluationResult.parse_failed=True."""
    fake_router = MagicMock()
    fake_router.generate_with_tier_fallback = AsyncMock(
        return_value=GenerationResult(
            text="抱歉，我无法以 JSON 形式输出这次评估结果。{{{",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            model="stub",
            provider="stub",
        )
    )

    with patch(
        "app.services.chapter_evaluator.get_model_router_async",
        AsyncMock(return_value=fake_router),
    ):
        result = await ChapterEvaluator().evaluate(
            chapter_text="本章测试内容。" * 50,
            chapter_outline={"summary": "测试章节大纲"},
        )

    assert result.parse_failed is True
    assert result.overall == 0.0


def test_should_revise_skips_parse_failed():
    """An untrusted (parse_failed) all-zero evaluation must NOT trigger a
    revision round; a trusted below-threshold score still must."""
    failed = EvaluationResult(overall=0.0, parse_failed=True)
    assert should_revise(failed, threshold=8.2) is False

    normal = EvaluationResult(overall=7.0, parse_failed=False)
    assert should_revise(normal, threshold=8.2) is True

    # Dict-shaped inputs (should_revise accepts both) honor the flag too.
    assert should_revise({"overall": 0.0, "parse_failed": True}, threshold=8.2) is False
    assert should_revise({"overall": 7.0}, threshold=8.2) is True
