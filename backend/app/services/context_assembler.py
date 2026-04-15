"""
Context Assembler

Assembles the system prompt for chapter generation by combining:
1. World state (settings, rules)
2. Outline (current chapter + book-level)
3. Short-term window (recent text)
4. User instructions

Phase 1 simplified version - no Qdrant/Neo4j dependency yet.
Full hierarchical memory (5-layer pyramid) will be added in Phase 3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Approximate tokens per Chinese character (conservative estimate)
CHARS_PER_TOKEN = 1.5

# Phase 1 token budget (will be expanded in Phase 3)
DEFAULT_TOKEN_BUDGET = 6000


@dataclass
class ContextComponents:
    """All components that make up the generation context."""
    world_state: str = ""       # World rules, settings
    outline: str = ""           # Current chapter outline + book outline summary
    recent_text: str = ""       # Short-term window (previous chapter + current content)
    style_instruction: str = "" # Style guidance (Phase 1: manual description)
    user_instruction: str = ""  # User's specific instruction for this generation
    hook_warnings: list[str] = field(default_factory=list)


@dataclass
class TokenBudget:
    """Token budget allocation for each context component."""
    world_state: int = 800
    outline: int = 500
    recent_text: int = 2500
    style_instruction: int = 500
    user_instruction: int = 500
    reserved: int = 200  # For formatting, separators, etc.

    @property
    def total(self) -> int:
        return (
            self.world_state
            + self.outline
            + self.recent_text
            + self.style_instruction
            + self.user_instruction
            + self.reserved
        )


class ContextAssembler:
    """
    Assembles the system prompt from multiple context sources.

    Phase 1: Uses PostgreSQL data only (settings, outlines, chapter text).
    Phase 3: Will add Qdrant (vector recall) + Neo4j (entity timeline) layers.
    """

    def __init__(self, token_budget: TokenBudget | None = None):
        self.budget = token_budget or TokenBudget()

    def assemble(self, components: ContextComponents) -> list[dict]:
        """
        Assemble context into a messages list for LLM.

        Returns:
            List of message dicts with system prompt and user message.
        """
        system_parts: list[str] = []

        # World state
        if components.world_state:
            truncated = self._truncate(components.world_state, self.budget.world_state)
            system_parts.append(f"【世界观设定】\n{truncated}")

        # Outline
        if components.outline:
            truncated = self._truncate(components.outline, self.budget.outline)
            system_parts.append(f"【当前大纲】\n{truncated}")

        # Recent text (short-term window)
        if components.recent_text:
            truncated = self._truncate(components.recent_text, self.budget.recent_text)
            system_parts.append(f"【近文上下文】\n{truncated}")

        # Style instruction
        if components.style_instruction:
            truncated = self._truncate(components.style_instruction, self.budget.style_instruction)
            system_parts.append(f"【风格要求】\n{truncated}")

        # Hook warnings
        if components.hook_warnings:
            warnings = "\n".join(f"- {w}" for w in components.hook_warnings)
            system_parts.append(f"【注意事项】\n{warnings}")

        system_prompt = "\n\n".join(system_parts)
        estimated_tokens = self._estimate_tokens(system_prompt)
        logger.info("Context assembled: ~%d tokens (budget: %d)", estimated_tokens, self.budget.total)

        messages = [{"role": "system", "content": system_prompt}]

        if components.user_instruction:
            messages.append({"role": "user", "content": components.user_instruction})
        else:
            messages.append({"role": "user", "content": "请根据以上设定和大纲，生成本章正文内容。"})

        return messages

    def _truncate(self, text: str, token_limit: int) -> str:
        """Truncate text to fit within token budget."""
        char_limit = int(token_limit * CHARS_PER_TOKEN)
        if len(text) <= char_limit:
            return text
        # Truncate from the beginning (keep recent content)
        return "...(前文已截断)...\n" + text[-char_limit:]

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate for Chinese text."""
        return int(len(text) / CHARS_PER_TOKEN)


def build_context_for_chapter(
    project_settings: dict,
    world_rules: list[str],
    book_outline_summary: str,
    chapter_outline: dict,
    previous_chapter_text: str,
    current_chapter_text: str,
    style_instruction: str = "",
    user_instruction: str = "",
) -> list[dict]:
    """
    Convenience function to build context for chapter generation.

    Args:
        project_settings: Project settings JSON
        world_rules: List of world rule texts
        book_outline_summary: Book-level outline summary
        chapter_outline: Current chapter's outline JSON
        previous_chapter_text: Full text of previous chapter
        current_chapter_text: Current chapter's existing text (if any)
        style_instruction: Style guidance
        user_instruction: User's specific instruction

    Returns:
        Messages list ready for LLM
    """
    import json

    # Build world state
    world_parts = []
    if project_settings:
        world_parts.append(json.dumps(project_settings, ensure_ascii=False, indent=2))
    for rule in world_rules:
        world_parts.append(rule)
    world_state = "\n".join(world_parts)

    # Build outline
    outline_parts = []
    if book_outline_summary:
        outline_parts.append(f"全书主线：{book_outline_summary}")
    if chapter_outline:
        outline_parts.append(f"本章大纲：\n{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}")
    outline = "\n\n".join(outline_parts)

    # Build recent text (short-term window)
    recent_parts = []
    if previous_chapter_text:
        recent_parts.append(f"【上一章内容】\n{previous_chapter_text}")
    if current_chapter_text:
        recent_parts.append(f"【本章已有内容】\n{current_chapter_text}")
    recent_text = "\n\n".join(recent_parts)

    assembler = ContextAssembler()
    components = ContextComponents(
        world_state=world_state,
        outline=outline,
        recent_text=recent_text,
        style_instruction=style_instruction,
        user_instruction=user_instruction or "请根据以上设定和大纲，生成本章正文内容。要求：内容完整连贯，人物言行符合人设，情节推进自然流畅。",
    )
    return assembler.assemble(components)
