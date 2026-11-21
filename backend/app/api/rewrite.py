"""Text rewrite and batch generation endpoints."""

from contextlib import aclosing

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory, get_db

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
    model_config = ConfigDict(extra='forbid')
    request_id: UUID | None = None
    project_id: str
    chapter_configs: list[dict]  # Each item includes the complete expected_content_text.
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
        try:
            rewriter = TextRewriter()
            yield f"data: {json.dumps({'status': 'rewriting'})}\n\n"

            async with aclosing(rewriter.rewrite_stream(selected_text=req.selected_text, operation=req.operation, custom_instruction=req.custom_instruction, context_before=req.context_before, context_after=req.context_after, max_tokens=req.max_tokens)) as owned_stream:
                async for chunk in owned_stream:
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            from app.services.rewrite_errors import rewrite_error_payload
            logger.warning("Rewrite failed: %s", type(e).__name__)
            yield f"data: {json.dumps(rewrite_error_payload(e), ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

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
    """Commit a fixed batch request before starting or replaying its stream."""
    from app.services.batch_submissions import BatchSubmissionError, submit_batch
    from app.services.generation_submissions import GenerationSubmissionError
    try:
        request_id = await submit_batch(db, req.project_id, req.request_id, req.chapter_configs, req.style_instruction)
    except (BatchSubmissionError, GenerationSubmissionError) as exc:
        raise HTTPException(exc.http_status, detail=exc.detail) from None
    project_id = str(UUID(req.project_id))

    async def event_stream() -> AsyncGenerator[str, None]:
        from app.services.batch_generator import BatchGenerator
        generator = BatchGenerator()

        # The SSE generator must own its own DB session: ``Depends(get_db)``
        # binds the session lifetime to the endpoint coroutine, which
        # returns as soon as StreamingResponse is built. Background work
        # (chapter generation + persistence) then races against the
        # session being closed and asyncpg connections get GC'd.
        try:
            async with async_session_factory() as db:
                yield f"data: {json.dumps({'status': 'accepted', 'job_id': request_id, 'request_id': request_id, 'project_id': project_id, 'total': len(req.chapter_configs)})}\n\n"

                # Stream progress events as they happen.
                queue: asyncio.Queue = asyncio.Queue()
                seen_terminal: set[str] = set()

                def on_progress(job) -> None:
                    for result in job.results:
                        if result.status in ("completed", "needs_review", "error", "cancelled") and result.chapter_id not in seen_terminal:
                            seen_terminal.add(result.chapter_id)
                            queue.put_nowait({
                                "chapter": result.chapter_idx,
                                "job_id": request_id,
                                "chapter_id": result.chapter_id,
                                "volume_idx": result.volume_idx,
                                "generation_task_id": result.generation_task_id,
                                "status": result.status,
                                "word_count": result.word_count,
                                "error": result.error,
                            })

                task = asyncio.create_task(
                    generator.run_submitted(
                        project_id=project_id,
                        request_id=request_id,
                        db=db,
                        on_progress=on_progress,
                    )
                )

                try:
                    while not task.done() or not queue.empty():
                        if not queue.empty():
                            yield f"data: {json.dumps(queue.get_nowait(), ensure_ascii=False)}\n\n"
                            continue
                        next_event = asyncio.create_task(queue.get())
                        try:
                            done, _ = await asyncio.wait((task, next_event), timeout=15,
                                                         return_when=asyncio.FIRST_COMPLETED)
                            if next_event in done:
                                yield f"data: {json.dumps(next_event.result(), ensure_ascii=False)}\n\n"
                            elif task in done:
                                break
                            else:
                                yield ": ping\n\n"
                        finally:
                            if not next_event.done():
                                next_event.cancel()
                            await asyncio.gather(next_event, return_exceptions=True)

                    job = await task
                    while not queue.empty():
                        ev = queue.get_nowait()
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

                    yield f"data: {json.dumps({'job_id': job.job_id, 'request_id': request_id, 'project_id': job.project_id, 'status': job.status.value, 'total': job.total_chapters, 'completed': job.completed_chapters, 'error': job.error}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


        except Exception as e:
            logger.warning("Batch stream ended without confirmation (%s)", type(e).__name__)
            detail = e.detail if isinstance(e, BatchSubmissionError) else {
                'code': 'batch_progress_unconfirmed', 'message': '批量进度尚未确认，请读取原批次，已保存稿件与原任务保留。'}
            yield f"data: {json.dumps({'status': 'unconfirmed', 'request_id': request_id, 'job_id': request_id, 'error': detail['message'], 'detail': detail}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get('/generate/batch/{request_id}')
async def get_batch_generation(request_id: UUID, project_id: UUID, db: AsyncSession = Depends(get_db)):
    """Read the original batch without starting or resuming any work."""
    from app.services.batch_submissions import BatchSubmissionError, batch_status
    try:
        return await batch_status(db, project_id, request_id)
    except BatchSubmissionError as exc:
        raise HTTPException(exc.http_status, detail=exc.detail) from None


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
    from sqlalchemy import select, func
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
