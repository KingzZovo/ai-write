"""
Chapter Generation Service (v0.5)

Uses ContextPackBuilder (L1 proximity + L2 facts + L3 RAG) and routes
through PromptRegistry so every call is logged in llm_call_logs.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.context_pack import ContextPackBuilder
from app.services.prompt_registry import run_text_prompt, stream_text_prompt

logger = logging.getLogger(__name__)


class ChapterGenerator:
    """Orchestrates chapter generation via ContextPack + PromptRegistry."""

    async def generate(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession,
        chapter_id: str | UUID | None = None,
        user_instruction: str = "",
    ) -> str:
        """Non-streaming: build pack, call run_text_prompt, return full text."""
        pack = await ContextPackBuilder(db=db).build(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
        )
        messages = pack.to_messages(user_instruction)
        rag_hits = self._collect_rag_hits(pack)

        result = await run_text_prompt(
            task_type="generation",
            user_content="",
            db=db,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            rag_hits=rag_hits,
            messages=messages,
        )
        return result.text

    async def generate_stream(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession,
        chapter_id: str | UUID | None = None,
        user_instruction: str = "",
    ) -> AsyncIterator[str]:
        """SSE streaming: build pack, stream through PromptRegistry."""
        pack = await ContextPackBuilder(db=db).build(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
        )
        messages = pack.to_messages(user_instruction)
        rag_hits = self._collect_rag_hits(pack)

        async for chunk in stream_text_prompt(
            task_type="generation",
            user_content="",
            db=db,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            rag_hits=rag_hits,
            messages=messages,
        ):
            yield chunk

    @staticmethod
    def _collect_rag_hits(pack) -> list[dict]:
        """Flatten ContextPack RAG layers into a serializable list for logging."""
        hits: list[dict] = []
        for s in pack.rag_snippets:
            hits.append({"collection": "chapter_summaries", "payload": {"summary": s}})
        for name, lines in pack.dialogue_samples.items():
            hits.append({
                "collection": "dialogue_samples",
                "payload": {"character": name, "lines": lines},
            })
        for s in pack.style_samples:
            hits.append({"collection": "style_samples", "payload": {"text": s}})
        return hits
