"""
Context Assembler (Phase 3 - Full Hierarchical Memory)

Assembles the system prompt for chapter generation by combining
all 5 layers of the memory pyramid:

L1: World Rules (永不衰减)        — ~800 tokens  (10%)
L2: Volume Summaries (卷级摘要)   — ~1500 tokens (19%)
L3: Chapter Summaries (章级摘要)  — ~1000 tokens (13%)
L4: Recent Text (短时窗口)        — ~2500 tokens (31%)
L5: Entity States (实体时间线)    — ~1000 tokens (13%)
+ Outline                        — ~500 tokens  (6%)
+ Style/Foreshadow               — ~400 tokens  (5%)
+ Instructions                   — ~300 tokens  (4%)

Total budget: ~8000 tokens for system prompt
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 1.5


@dataclass
class ContextComponents:
    """All components that make up the generation context."""
    # L1: World state
    world_state: str = ""
    # L2: Volume summaries (all volumes)
    volume_summaries: str = ""
    # L3: Chapter summaries
    chapter_summaries: str = ""
    # L4: Short-term window
    recent_text: str = ""
    # L5: Entity states
    entity_states: str = ""
    # Outline
    outline: str = ""
    # Style
    style_instruction: str = ""
    # Foreshadow prompts
    foreshadow_hints: str = ""
    # Hook warnings
    hook_warnings: list[str] = field(default_factory=list)
    # User instruction
    user_instruction: str = ""


@dataclass
class TokenBudget:
    """Token budget allocation for the 8000-token system prompt."""
    world_state: int = 800       # L1
    volume_summaries: int = 1500  # L2
    chapter_summaries: int = 1000 # L3
    recent_text: int = 2500      # L4
    entity_states: int = 1000    # L5
    outline: int = 500
    style_instruction: int = 300
    foreshadow_hints: int = 200
    user_instruction: int = 300
    reserved: int = 100

    @property
    def total(self) -> int:
        return (
            self.world_state + self.volume_summaries + self.chapter_summaries
            + self.recent_text + self.entity_states + self.outline
            + self.style_instruction + self.foreshadow_hints
            + self.user_instruction + self.reserved
        )

    # Priority order for truncation when over budget
    # (lower priority = truncated first)
    TRUNCATION_ORDER = [
        "chapter_summaries",    # Truncate historical chapter summaries first
        "volume_summaries",     # Then older volume summaries
        "entity_states",        # Then entity states
        "outline",              # Then outline details
        "foreshadow_hints",     # Then foreshadow hints
        # Never truncate: recent_text, world_state, user_instruction
    ]


class ContextAssembler:
    """
    Assembles the system prompt from the 5-layer memory pyramid.

    Supports both Phase 1 (simple) and Phase 3 (full hierarchy) modes.
    """

    def __init__(self, token_budget: TokenBudget | None = None):
        self.budget = token_budget or TokenBudget()

    def assemble(self, components: ContextComponents) -> list[dict]:
        """Assemble all context layers into a messages list for LLM."""
        sections: list[tuple[str, str, int]] = []  # (label, content, budget)

        # L1: World state (always first, always full)
        if components.world_state:
            sections.append((
                "\u3010\u4e16\u754c\u89c2\u8bbe\u5b9a\u3011",
                components.world_state,
                self.budget.world_state,
            ))

        # L5: Entity states (characters, relationships at current point)
        if components.entity_states:
            sections.append((
                "\u3010\u5f53\u524d\u89d2\u8272\u72b6\u6001\u3011",
                components.entity_states,
                self.budget.entity_states,
            ))

        # L2: Volume summaries (全部卷级摘要)
        if components.volume_summaries:
            sections.append((
                "\u3010\u5386\u53f2\u5377\u7ea7\u6458\u8981\u3011",
                components.volume_summaries,
                self.budget.volume_summaries,
            ))

        # L3: Chapter summaries
        if components.chapter_summaries:
            sections.append((
                "\u3010\u7ae0\u8282\u6458\u8981\u3011",
                components.chapter_summaries,
                self.budget.chapter_summaries,
            ))

        # Outline
        if components.outline:
            sections.append((
                "\u3010\u5f53\u524d\u5927\u7eb2\u3011",
                components.outline,
                self.budget.outline,
            ))

        # L4: Recent text (highest priority, largest budget)
        if components.recent_text:
            sections.append((
                "\u3010\u8fd1\u6587\u4e0a\u4e0b\u6587\u3011",
                components.recent_text,
                self.budget.recent_text,
            ))

        # Style instruction
        if components.style_instruction:
            sections.append((
                "\u3010\u98ce\u683c\u8981\u6c42\u3011",
                components.style_instruction,
                self.budget.style_instruction,
            ))

        # Foreshadow hints
        if components.foreshadow_hints:
            sections.append((
                "\u3010\u4f0f\u7b14\u63d0\u793a\u3011",
                components.foreshadow_hints,
                self.budget.foreshadow_hints,
            ))

        # Hook warnings
        if components.hook_warnings:
            warnings = "\n".join(f"- {w}" for w in components.hook_warnings)
            sections.append((
                "\u3010\u6ce8\u610f\u4e8b\u9879\u3011",
                warnings,
                200,
            ))

        # Build system prompt with truncation
        system_parts: list[str] = []
        for label, content, budget in sections:
            truncated = self._truncate(content, budget)
            system_parts.append(f"{label}\n{truncated}")

        system_prompt = "\n\n".join(system_parts)
        estimated_tokens = self._estimate_tokens(system_prompt)
        logger.info(
            "Context assembled: ~%d tokens (budget: %d), %d sections",
            estimated_tokens,
            self.budget.total,
            len(sections),
        )

        messages = [{"role": "system", "content": system_prompt}]

        if components.user_instruction:
            messages.append({"role": "user", "content": components.user_instruction})
        else:
            messages.append({
                "role": "user",
                "content": "\u8bf7\u6839\u636e\u4ee5\u4e0a\u8bbe\u5b9a\u548c\u5927\u7eb2\uff0c\u751f\u6210\u672c\u7ae0\u6b63\u6587\u5185\u5bb9\u3002",
            })

        return messages

    def _truncate(self, text: str, token_limit: int) -> str:
        """Truncate text to fit within token budget."""
        char_limit = int(token_limit * CHARS_PER_TOKEN)
        if len(text) <= char_limit:
            return text
        return "...(\u524d\u6587\u5df2\u622a\u65ad)...\n" + text[-char_limit:]

    def _estimate_tokens(self, text: str) -> int:
        return int(len(text) / CHARS_PER_TOKEN)


# =========================================================================
# Convenience function (Phase 1 compatible + Phase 3 full)
# =========================================================================

def build_context_for_chapter(
    project_settings: dict | None = None,
    world_rules: list[str] | None = None,
    book_outline_summary: str = "",
    chapter_outline: dict | None = None,
    previous_chapter_text: str = "",
    current_chapter_text: str = "",
    style_instruction: str = "",
    user_instruction: str = "",
    # Phase 3 additions
    volume_summaries: str = "",
    chapter_summaries: str = "",
    entity_states: str = "",
    foreshadow_hints: str = "",
    hook_warnings: list[str] | None = None,
) -> list[dict]:
    """
    Build context for chapter generation.

    Supports both Phase 1 (basic fields) and Phase 3 (full memory pyramid).
    """
    # L1: World state
    world_parts: list[str] = []
    if project_settings:
        world_parts.append(json.dumps(project_settings, ensure_ascii=False, indent=2))
    for rule in (world_rules or []):
        world_parts.append(rule)
    world_state = "\n".join(world_parts)

    # Outline
    outline_parts: list[str] = []
    if book_outline_summary:
        outline_parts.append(f"\u5168\u4e66\u4e3b\u7ebf\uff1a{book_outline_summary}")
    if chapter_outline:
        outline_parts.append(
            f"\u672c\u7ae0\u5927\u7eb2\uff1a\n{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}"
        )
    outline = "\n\n".join(outline_parts)

    # L4: Recent text
    recent_parts: list[str] = []
    if previous_chapter_text:
        recent_parts.append(f"\u3010\u4e0a\u4e00\u7ae0\u5185\u5bb9\u3011\n{previous_chapter_text}")
    if current_chapter_text:
        recent_parts.append(f"\u3010\u672c\u7ae0\u5df2\u6709\u5185\u5bb9\u3011\n{current_chapter_text}")
    recent_text = "\n\n".join(recent_parts)

    assembler = ContextAssembler()
    components = ContextComponents(
        world_state=world_state,
        volume_summaries=volume_summaries,
        chapter_summaries=chapter_summaries,
        recent_text=recent_text,
        entity_states=entity_states,
        outline=outline,
        style_instruction=style_instruction,
        foreshadow_hints=foreshadow_hints,
        hook_warnings=hook_warnings or [],
        user_instruction=user_instruction or (
            "\u8bf7\u6839\u636e\u4ee5\u4e0a\u8bbe\u5b9a\u548c\u5927\u7eb2\uff0c\u751f\u6210\u672c\u7ae0\u6b63\u6587\u5185\u5bb9\u3002"
            "\u8981\u6c42\uff1a\u5185\u5bb9\u5b8c\u6574\u8fde\u8d2f\uff0c\u4eba\u7269\u8a00\u884c\u7b26\u5408\u4eba\u8bbe\uff0c\u60c5\u8282\u63a8\u8fdb\u81ea\u7136\u6d41\u7545\u3002"
        ),
    )
    return assembler.assemble(components)
