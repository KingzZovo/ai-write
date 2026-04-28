"""v1.7.4 P0-2 unit tests for chapter_summarizer service.

Key contract: the public _clean_summary_output helper must strip markdown
fences and JSON wrappers so that summaries written into chapter.summary are
pure prose (no ```json fences, no \"summary\": fields, no quotes/backticks).
If this regresses, ContextPack.recent_summaries gets polluted and downstream
chapters generate with garbled context.
"""
from __future__ import annotations

import pytest

from app.services.chapter_summarizer import _clean_summary_output


class TestCleanSummaryOutput:
    def test_plain_returns_as_is(self):
        assert _clean_summary_output("纪砥在雨夜。") == "纪砥在雨夜。"

    def test_empty_returns_empty(self):
        assert _clean_summary_output("") == ""
        assert _clean_summary_output("   \n  ") == ""

    def test_strips_json_fence(self):
        raw = '```json\n{"summary":"雨夜。"}\n```'
        assert _clean_summary_output(raw) == "雨夜。"

    def test_strips_text_fence(self):
        raw = "```text\n净文本。\n```"
        assert _clean_summary_output(raw) == "净文本。"

    def test_strips_unlabeled_fence(self):
        raw = "```\n问调记录。\n```"
        assert _clean_summary_output(raw) == "问调记录。"

    def test_extracts_summary_from_bare_json(self):
        raw = '{"summary":"薄雾中的名字。"}'
        assert _clean_summary_output(raw) == "薄雾中的名字。"

    def test_extracts_summary_from_pretty_json(self):
        raw = '{\n  "summary": "薄雾中的名字。"\n}'
        assert _clean_summary_output(raw) == "薄雾中的名字。"

    def test_strips_fence_then_json_combined(self):
        """Real-world model output: fenced JSON."""
        raw = '```json\n{\n  "summary": "雨夜纪砥陷入禁库，烟佭退散。"\n}\n```'
        assert _clean_summary_output(raw) == "雨夜纪砥陷入禁库，烟佭退散。"

    def test_falls_back_to_regex_for_invalid_json(self):
        # Some models produce pseudo-JSON with unescaped quotes/newlines that
        # json.loads can't parse — the regex fallback should still extract.
        raw = '{"summary": "纪砥 \\"薄雾\\" 之事。"}'
        out = _clean_summary_output(raw)
        assert "纪砥" in out and "summary" not in out

    def test_keeps_first_paragraph_only(self):
        raw = "第一段内容。\n\n第二段不应出现。"
        assert _clean_summary_output(raw) == "第一段内容。"

    def test_strips_residual_quotes(self):
        raw = '"纪砥被凌祝拍走。"'
        assert _clean_summary_output(raw) == "纪砥被凌祝拍走。"

    def test_handles_non_dict_json_gracefully(self):
        # Array JSON shouldn't crash; we'll fall through and return as-is
        # after fence/quote stripping (or empty if nothing remains).
        raw = '["a", "b"]'
        out = _clean_summary_output(raw)
        # should not raise; output is a string (possibly empty/the original)
        assert isinstance(out, str)


class TestSummarizeChapterTextGuards:
    """Guard tests for summarize_chapter_text without hitting the real LLM."""

    @pytest.mark.asyncio
    async def test_returns_empty_for_short_content(self):
        from app.services.chapter_summarizer import summarize_chapter_text
        out = await summarize_chapter_text(
            title="测试",
            content_text="很短\n\n不够200字\u3002",
            db=None,  # type: ignore[arg-type]
        )
        assert out == ""

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_content(self):
        from app.services.chapter_summarizer import summarize_chapter_text
        out = await summarize_chapter_text(title="x", content_text="", db=None)  # type: ignore[arg-type]
        assert out == ""
