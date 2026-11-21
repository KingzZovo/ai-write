"""
Chapter Generation Service (v0.5)

Uses ContextPackBuilder (L1 proximity + L2 facts + L3 RAG) and routes
through PromptRegistry so every call is logged in llm_call_logs.
"""

from __future__ import annotations

from contextlib import aclosing

import hashlib
import logging
from typing import AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.generation_context import generation_context_session
from app.services.context_pack import ContextPackBuilder
from app.services.narrative_contract import NARRATIVE_CONTRACT_VERSION
from app.services.narrative_quality_gates import BLUEPRINT_VERSION, preflight_scene_blueprint_prompt
from app.services.outline_readiness import build_outline_readiness_report
from app.services.prompt_registry import run_text_prompt, stream_text_prompt

logger = logging.getLogger(__name__)


class ChapterGenerator:
    """Orchestrates chapter generation via ContextPack + PromptRegistry.

    Pass db=None for owned short context reads. A borrowed transaction is
    never implicitly committed or discarded for deferred semantic recall.
    """

    async def generate(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession | None,
        chapter_id: str | UUID | None = None,
        user_instruction: str = "",
        request_timeout: float | None = None,
        retry_attempts: int | None = None,
        stream: bool | None = None,
    ) -> str:
        """Non-streaming: build pack, call run_text_prompt, return full text.

        Long single-shot chapter generation can legitimately take longer than
        the global chat timeout. Callers may pass per-call routing kwargs so
        the timeout budget used by the outer task guard also reaches the LLM
        request/stream collection layer.
        """
        async with generation_context_session(db) as context_db:
            await self._assert_outline_chain_ready(
                db=context_db,
                project_id=project_id,
                volume_id=volume_id,
                chapter_idx=chapter_idx,
                chapter_id=chapter_id,
            )
            pack = await ContextPackBuilder(db=context_db).build(
                project_id=project_id,
                volume_id=volume_id,
                chapter_idx=chapter_idx,
            )
        await pack.resolve_cognition(db=db, user_instruction=user_instruction)
        messages = self._with_preflight_blueprint(
            pack.to_messages(user_instruction),
            chapter_idx=chapter_idx,
        )
        rag_hits = self._collect_rag_hits(pack)

        llm_kwargs: dict[str, object] = {}
        if request_timeout is not None:
            llm_kwargs["request_timeout"] = request_timeout
        if retry_attempts is not None:
            llm_kwargs["retry_attempts"] = retry_attempts
        if stream is not None:
            llm_kwargs["stream"] = stream

        result = await run_text_prompt(
            task_type="generation",
            user_content="",
            db=db,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            rag_hits=rag_hits,
            messages=messages,
            **llm_kwargs,
        )
        from app.services.provider_response_guard import require_normal_finish
        require_normal_finish(getattr(result, "finish_reason", None))
        text = result.text

        if text and text.strip():
            if text.strip()[-1] not in "。！？」》…":
                logger.warning(
                    "chapter_generator truncation project=%s ch=%d ends_with=%r",
                    project_id, chapter_idx, text[-50:],
                )
            try:
                missing = await _check_character_names_in_text(db, str(project_id), text)
                if missing:
                    logger.warning(
                        "chapter_generator missing_chars project=%s ch=%d names=%s",
                        project_id, chapter_idx, missing,
                    )
            except Exception:
                pass

        return text

    async def generate_stream(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession | None,
        chapter_id: str | UUID | None = None,
        user_instruction: str = "",
    ) -> AsyncIterator[str]:
        """SSE streaming: build pack, stream through PromptRegistry."""
        async with generation_context_session(db) as context_db:
            await self._assert_outline_chain_ready(
                db=context_db,
                project_id=project_id,
                volume_id=volume_id,
                chapter_idx=chapter_idx,
                chapter_id=chapter_id,
            )
            pack = await ContextPackBuilder(db=context_db).build(
                project_id=project_id,
                volume_id=volume_id,
                chapter_idx=chapter_idx,
            )
        await pack.resolve_cognition(db=db, user_instruction=user_instruction)
        messages = self._with_preflight_blueprint(
            pack.to_messages(user_instruction),
            chapter_idx=chapter_idx,
        )
        rag_hits = self._collect_rag_hits(pack)

        async with aclosing(stream_text_prompt(task_type='generation', user_content='', db=db, project_id=str(project_id), chapter_id=str(chapter_id) if chapter_id else None, rag_hits=rag_hits, messages=messages)) as owned_stream:
            async for chunk in owned_stream:
                yield chunk

    @staticmethod
    def _with_preflight_blueprint(messages: list[dict], *, chapter_idx: int) -> list[dict]:
        """Inject author-defined outcomes and continuity checks before drafting."""
        blueprint = preflight_scene_blueprint_prompt(chapter_idx=chapter_idx)
        prompt_hash = hashlib.sha256(blueprint.encode("utf-8")).hexdigest()[:16]
        markers = (
            f"direct_generation_first_{BLUEPRINT_VERSION}",
            "author_outcomes", "continuity_state", "causal_connections",
            "narrative_purpose", "completion_check", "chinese_prose_mechanics",
            "cross_project_prose_quality_contract",
        )
        missing = [marker for marker in markers if marker not in blueprint]
        logger.warning(
            "preflight_contract_snapshot contract_version=%s chapter_idx=%s prompt_hash=%s markers_missing=%s",
            NARRATIVE_CONTRACT_VERSION,
            chapter_idx,
            prompt_hash,
            ",".join(missing) if missing else "none",
        )
        return [{"role": "system", "content": blueprint}, *messages]

    @staticmethod
    async def _assert_outline_chain_ready(
        *,
        db: AsyncSession | None,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        chapter_id: str | UUID | None = None,
    ) -> None:
        readiness = await build_outline_readiness_report(
            db,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            volume_id=str(volume_id),
            chapter_idx=chapter_idx,
        )
        if not readiness.ready:
            missing = ",".join(readiness.missing_layers)
            raise RuntimeError(
                f"outline_chain_incomplete: {readiness.block_message()} ({missing})"
            )

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


async def _check_character_names_in_text(
    db: AsyncSession | None, project_id: str, text: str
) -> list[str]:
    from app.models.project import Character

    stmt = select(Character.name).where(Character.project_id == project_id)
    async with generation_context_session(db) as read_db:
        res = await read_db.execute(stmt)
        names = [row[0] for row in res.all() if row[0]]
    return [n for n in names if n not in text]
