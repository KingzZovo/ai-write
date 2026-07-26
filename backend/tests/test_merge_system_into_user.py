"""Endpoint workaround: fold system messages into the first user message.

Some relay channels (observed 2026-07-26: relay claude-* wrapped by an agent
shell) drop or replace the system role, so every generation task that ships
context via system silently loses it — the model then complains "no story
context" and the pipeline degrades. The workaround folds system content into
the first user message for models matched by
``settings.LLM_MERGE_SYSTEM_INTO_USER_MODELS`` (comma-separated substrings).
"""

from app.services.model_router import (
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
