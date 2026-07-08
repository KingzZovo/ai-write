"""Behavior tests for the two cognition-ledger prompt injection points.

Q3 review fix: mutation testing showed both injection branches
(`ContextPack.to_system_prompt`'s `if self.cognition_boundaries:` and
`chapter_evaluator._build_user_prompt`'s `if cognition_ledger_text:`) could
be disabled with the full suite still green. These tests pin the actual
prompt output so the wiring cannot silently regress.
"""
from __future__ import annotations

from app.services.chapter_evaluator import _build_user_prompt
from app.services.context_pack import ContextPack

LEDGER_TEXT = "林冲知道：高俅设局"


class TestContextPackCognitionInjection:
    def test_system_prompt_contains_cognition_boundary_section(self):
        pack = ContextPack(cognition_boundaries=LEDGER_TEXT)
        prompt = pack.to_system_prompt(token_budget=8000)
        assert "【人物认知边界】" in prompt
        assert LEDGER_TEXT in prompt

    def test_system_prompt_omits_section_when_empty(self):
        pack = ContextPack(cognition_boundaries="")
        prompt = pack.to_system_prompt(token_budget=8000)
        assert "【人物认知边界】" not in prompt


class TestEvaluatorCognitionInjection:
    @staticmethod
    def _build(ledger_text: str) -> str:
        return _build_user_prompt(
            chapter_text="林冲连夜奔走，雪夜上山。",
            chapter_outline={"title": "第1章"},
            previous_summary="",
            style_profile="",
            active_foreshadows=None,
            cognition_ledger_text=ledger_text,
        )

    def test_user_prompt_contains_ledger_section(self):
        prompt = self._build(LEDGER_TEXT)
        assert "当前认知账本" in prompt
        assert LEDGER_TEXT in prompt

    def test_user_prompt_omits_ledger_section_when_empty(self):
        prompt = self._build("")
        assert "当前认知账本" not in prompt
        assert LEDGER_TEXT not in prompt
