"""
Chapter Generation Service

Orchestrates the full chapter generation pipeline:
1. Build context (ContextAssembler)
2. Generate draft (PlotAgent)
3. Polish with style (StyleAgent)
4. Save result
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from app.services.context_assembler import build_context_for_chapter
from app.services.agents.plot_agent import PlotAgent
from app.services.agents.style_agent import StyleAgent

logger = logging.getLogger(__name__)


class ChapterGenerator:
    """Orchestrates chapter generation through the dual-agent pipeline."""

    def __init__(self):
        self.plot_agent = PlotAgent()
        self.style_agent = StyleAgent()

    async def generate(
        self,
        project_settings: dict,
        world_rules: list[str],
        book_outline_summary: str,
        chapter_outline: dict,
        previous_chapter_text: str,
        current_chapter_text: str = "",
        style_instruction: str = "",
        user_instruction: str = "",
        max_tokens: int = 4096,
        skip_polish: bool = False,
    ) -> str:
        """
        Generate a full chapter through the dual-agent pipeline.

        Args:
            project_settings: Project settings JSON
            world_rules: List of world rule texts
            book_outline_summary: Book-level outline summary
            chapter_outline: Current chapter's outline
            previous_chapter_text: Previous chapter's full text
            current_chapter_text: Current chapter's existing text
            style_instruction: Style guidance
            user_instruction: User's specific instruction
            max_tokens: Max tokens for generation
            skip_polish: If True, skip the StyleAgent step

        Returns:
            Final chapter text
        """
        # Step 1: Build context
        context_messages = build_context_for_chapter(
            project_settings=project_settings,
            world_rules=world_rules,
            book_outline_summary=book_outline_summary,
            chapter_outline=chapter_outline,
            previous_chapter_text=previous_chapter_text,
            current_chapter_text=current_chapter_text,
            style_instruction="" if not skip_polish else style_instruction,
            user_instruction=user_instruction,
        )

        # Step 2: Generate draft via PlotAgent
        logger.info("PlotAgent: generating draft...")
        draft = await self.plot_agent.generate(context_messages, max_tokens=max_tokens)
        logger.info("PlotAgent: draft generated (%d chars)", len(draft))

        if skip_polish or not style_instruction:
            return draft

        # Step 3: Polish via StyleAgent
        logger.info("StyleAgent: polishing draft...")
        polished = await self.style_agent.polish(
            draft_text=draft,
            style_instruction=style_instruction,
            max_tokens=max_tokens,
        )
        logger.info("StyleAgent: polished (%d chars)", len(polished))

        return polished

    async def generate_stream(
        self,
        project_settings: dict,
        world_rules: list[str],
        book_outline_summary: str,
        chapter_outline: dict,
        previous_chapter_text: str,
        current_chapter_text: str = "",
        style_instruction: str = "",
        user_instruction: str = "",
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """
        Generate chapter with streaming output.

        For streaming, we run PlotAgent in non-streaming mode first,
        then stream the StyleAgent output. If no style instruction,
        we stream PlotAgent directly.
        """
        context_messages = build_context_for_chapter(
            project_settings=project_settings,
            world_rules=world_rules,
            book_outline_summary=book_outline_summary,
            chapter_outline=chapter_outline,
            previous_chapter_text=previous_chapter_text,
            current_chapter_text=current_chapter_text,
            user_instruction=user_instruction,
        )

        if not style_instruction:
            # Stream PlotAgent directly
            async for chunk in self.plot_agent.generate_stream(
                context_messages, max_tokens=max_tokens
            ):
                yield chunk
        else:
            # Generate draft first, then stream polish
            draft = await self.plot_agent.generate(context_messages, max_tokens=max_tokens)
            async for chunk in self.style_agent.polish_stream(
                draft_text=draft,
                style_instruction=style_instruction,
                max_tokens=max_tokens,
            ):
                yield chunk
