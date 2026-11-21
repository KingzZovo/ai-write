"""Endpoint workaround: fold system messages into the first user message.

Some relay channels (observed 2026-07-26: relay claude-* wrapped by an agent
shell) drop or replace the system role, so every generation task that ships
context via system silently loses it — the model then complains "no story
context" and the pipeline degrades. The workaround folds system content into
the first user message for models matched by
``settings.LLM_MERGE_SYSTEM_INTO_USER_MODELS`` (comma-separated substrings).
"""

import asyncio
from types import SimpleNamespace

from app.services.model_router import (
    OpenAIProvider,
    _should_merge_system_for_model,
    merge_system_into_user,
)


class TestShouldMerge:
    def test_empty_setting_never_merges(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_MERGE_SYSTEM_INTO_USER_MODELS", "")
        assert _should_merge_system_for_model("claude-sonnet-5") is False

    def test_substring_match(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_MERGE_SYSTEM_INTO_USER_MODELS", "claude-")
        assert _should_merge_system_for_model("claude-sonnet-5") is True
        assert _should_merge_system_for_model("claude-opus-5") is True
        assert _should_merge_system_for_model("gemini-3.1-pro") is False

    def test_multiple_patterns(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_MERGE_SYSTEM_INTO_USER_MODELS", "claude-, grok")
        assert _should_merge_system_for_model("grok-4") is True
        assert _should_merge_system_for_model("gemini-3.5-flash") is False


class TestMergeSystemIntoUser:
    def test_no_system_returns_unchanged(self):
        msgs = [{"role": "user", "content": "写一段"}]
        assert merge_system_into_user(msgs) == msgs

    def test_system_folded_into_first_user(self):
        msgs = [
            {"role": "system", "content": "你是小说写手。主角叫虞千帆。"},
            {"role": "user", "content": "写第一场。"},
        ]
        out = merge_system_into_user(msgs)
        assert all(m["role"] != "system" for m in out)
        assert len(out) == 1
        assert "虞千帆" in out[0]["content"]
        assert "写第一场。" in out[0]["content"]
        # system context must come BEFORE the user ask
        assert out[0]["content"].index("虞千帆") < out[0]["content"].index("写第一场。")

    def test_multiple_system_blocks_joined(self):
        msgs = [
            {"role": "system", "content": "规则A"},
            {"role": "system", "content": "规则B"},
            {"role": "user", "content": "问题"},
        ]
        out = merge_system_into_user(msgs)
        assert len(out) == 1
        c = out[0]["content"]
        assert "规则A" in c and "规则B" in c and "问题" in c

    def test_system_only_becomes_user(self):
        msgs = [{"role": "system", "content": "只有系统"}]
        out = merge_system_into_user(msgs)
        assert len(out) == 1
        assert out[0]["role"] == "user"
        assert "只有系统" in out[0]["content"]

    def test_assistant_history_preserved_in_order(self):
        msgs = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
        ]
        out = merge_system_into_user(msgs)
        assert [m["role"] for m in out] == ["user", "assistant", "user"]
        assert "S" in out[0]["content"] and "U1" in out[0]["content"]
        assert out[1]["content"] == "A1"
        assert out[2]["content"] == "U2"

    def test_original_messages_not_mutated(self):
        msgs = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
        ]
        merge_system_into_user(msgs)
        assert msgs[0]["content"] == "S"
        assert msgs[1]["content"] == "U"


def _fake_openai_client(captured: dict):
    """Fake AsyncOpenAI client capturing chat.completions.create kwargs."""

    async def create(**kwargs):
        captured.update(kwargs)
        if kwargs.get("stream"):
            async def _aiter():
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="ok"))])
            return _aiter()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2),
        )

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


class TestOpenAIProviderWiring:
    """The merge must actually run on the OpenAIProvider request path."""

    MSGS = [
        {"role": "system", "content": "主角叫虞千帆。"},
        {"role": "user", "content": "写第一场。"},
    ]

    def _provider(self, captured: dict) -> OpenAIProvider:
        provider = OpenAIProvider(api_key="k", base_url="")
        provider._client = _fake_openai_client(captured)
        return provider

    def test_generate_merges_for_matching_model(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_MERGE_SYSTEM_INTO_USER_MODELS", "claude-")
        captured: dict = {}
        asyncio.run(self._provider(captured).generate(
            self.MSGS, model="claude-sonnet-5", stream=False))
        sent = captured["messages"]
        assert all(m["role"] != "system" for m in sent)
        assert "虞千帆" in sent[0]["content"]
        assert "写第一场。" in sent[0]["content"]

    def test_generate_keeps_system_for_non_matching_model(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_MERGE_SYSTEM_INTO_USER_MODELS", "claude-")
        captured: dict = {}
        asyncio.run(self._provider(captured).generate(
            self.MSGS, model="gpt-4o", stream=False))
        sent = captured["messages"]
        assert sent[0]["role"] == "system"
        assert sent is self.MSGS or sent == self.MSGS

    def test_generate_stream_merges_for_matching_model(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_MERGE_SYSTEM_INTO_USER_MODELS", "claude-")
        captured: dict = {}

        async def _consume():
            async for _ in self._provider(captured).generate_stream(
                    self.MSGS, model="claude-sonnet-5"):
                pass

        asyncio.run(_consume())
        sent = captured["messages"]
        assert all(m["role"] != "system" for m in sent)
        assert "虞千帆" in sent[0]["content"]
