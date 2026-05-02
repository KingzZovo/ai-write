"""Text rewrite and batch generation endpoints."""

import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["rewrite"])


class RewriteRequest(BaseModel):
    selected_text: str
    operation: str  # condense, expand, restructure, continue, custom
    custom_instruction: str = ""
    context_before: str = ""
    context_after: str = ""
    max_tokens: int = 2048


class BatchGenerateRequest(BaseModel):
    project_id: str
    chapter_configs: list[dict]  # [{chapter_id, volume_id, chapter_idx, outline}]
    style_instruction: str = ""


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/rewrite")
async def rewrite_text(req: RewriteRequest) -> StreamingResponse:
    """Rewrite selected text via SSE streaming."""

    async def event_stream() -> AsyncGenerator[str, None]:
        from app.services.text_rewriter import TextRewriter
        rewriter = TextRewriter()

        try:
            yield f"data: {json.dumps({'status': 'rewriting'})}\n\n"

            async for chunk in rewriter.rewrite_stream(
                selected_text=req.selected_text,
                operation=req.operation,
                custom_instruction=req.custom_instruction,
                context_before=req.context_before,
                context_after=req.context_after,
                max_tokens=req.max_tokens,
            ):
                yield f"data: {json.dumps({'text': chunk})}\n\n"

            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Rewrite failed")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/generate/batch")
async def batch_generate(
    req: BatchGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Batch generate multiple chapters with progress via SSE."""

    async def event_stream() -> AsyncGenerator[str, None]:
        from app.services.batch_generator import BatchGenerator
        generator = BatchGenerator()

        try:
            yield f"data: {json.dumps({'status': 'starting', 'total': len(req.chapter_configs)})}\n\n"

            def on_progress(job):
                pass  # Progress sent via SSE below

            job = await generator.generate_batch(
                project_id=req.project_id,
                chapter_configs=req.chapter_configs,
                style_instruction=req.style_instruction,
            )

            for result in job.results:
                yield f"data: {json.dumps({'chapter': result.chapter_idx, 'status': result.status, 'word_count': result.word_count, 'error': result.error})}\n\n"

            yield f"data: {json.dumps({'status': 'completed', 'total': job.total_chapters, 'completed': job.completed_chapters})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Batch generation failed")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/stats/tokens")
async def get_token_stats(
    project_id: str | None = None,
    since_hours: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get token usage statistics.

    Reads from the persistent ``llm_call_logs`` table so the value survives
    backend restarts (the legacy in-memory counter from
    ``ModelRouter.get_usage_stats()`` is also returned for compatibility).

    Query params:
      project_id: optional filter to a single project.
      since_hours: optional time window (e.g. 24, 168). Default = all-time.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select, func, text as _text
    from app.models.call_log import LLMCallLog as LlmCallLog
    from app.services.model_router import get_model_router
    from app.services.semantic_cache import SemanticCache

    q = select(
        func.count(LlmCallLog.id).label("calls"),
        func.coalesce(func.sum(LlmCallLog.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(LlmCallLog.output_tokens), 0).label("output_tokens"),
    )
    if project_id:
        q = q.where(LlmCallLog.project_id == project_id)
    if since_hours and since_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
        q = q.where(LlmCallLog.created_at >= cutoff)
    row = (await db.execute(q)).one()
    total_input = int(row.input_tokens or 0)
    total_output = int(row.output_tokens or 0)

    # Per-task breakdown (limited to the same scope).
    bq = select(
        LlmCallLog.task_type,
        func.count(LlmCallLog.id).label("calls"),
        func.coalesce(func.sum(LlmCallLog.input_tokens), 0).label("i"),
        func.coalesce(func.sum(LlmCallLog.output_tokens), 0).label("o"),
    ).group_by(LlmCallLog.task_type).order_by(func.count(LlmCallLog.id).desc())
    if project_id:
        bq = bq.where(LlmCallLog.project_id == project_id)
    if since_hours and since_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
        bq = bq.where(LlmCallLog.created_at >= cutoff)
    by_task = [
        {
            "task_type": r.task_type,
            "calls": int(r.calls),
            "input_tokens": int(r.i or 0),
            "output_tokens": int(r.o or 0),
        }
        for r in (await db.execute(bq)).all()
    ]

    cache = SemanticCache()
    legacy = get_model_router().get_usage_stats()

    return {
        "token_usage": {
            "total_calls": int(row.calls or 0),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "by_task_type": by_task,
            "scope": {
                "project_id": project_id,
                "since_hours": since_hours,
            },
        },
        "token_usage_inmemory": legacy,
        "cache_stats": cache.get_stats(),
    }
