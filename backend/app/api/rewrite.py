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

# ============================================================================
# Token statistics — sourced from llm_call_logs (DB) instead of in-memory
# router counters that reset on every process restart.
# ============================================================================

from datetime import datetime, timedelta, timezone as _tz
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, and_


def _parse_since(since: str | None) -> datetime | None:
    """Parse a relative window like '7d' / '24h' / '90m', an ISO-8601 string,
    or None. Returns a tz-aware datetime in UTC, or None if cannot parse.
    """
    if not since:
        return None
    s = since.strip().lower()
    try:
        if s.endswith("d"):
            return datetime.now(_tz.utc) - timedelta(days=int(s[:-1]))
        if s.endswith("h"):
            return datetime.now(_tz.utc) - timedelta(hours=int(s[:-1]))
        if s.endswith("m"):
            return datetime.now(_tz.utc) - timedelta(minutes=int(s[:-1]))
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("/stats/tokens")
async def get_token_stats(
    project_id: str | None = None,
    since: str | None = None,
    task_type: str | None = None,
    group_by: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aggregate token usage from the ``llm_call_logs`` DB table.

    Query params:
      - ``project_id``: scope to one project. Omit for global stats.
      - ``since``: relative window ("7d" / "24h" / "90m") or ISO-8601 datetime.
      - ``task_type``: filter by a single task type (e.g. "chapter", "outline").
      - ``group_by``: one of ``task_type`` / ``tier`` / ``model``. When set,
        an additional ``breakdown`` array is returned keyed by that field.

    Backwards-compatible response shape (used by ``TokenDashboard.tsx``):
      ``totalInputTokens`` / ``totalOutputTokens`` / ``totalTokens`` /
      ``cacheHits`` / ``cacheMisses`` / ``cacheHitRate`` are top-level keys.
    The richer payload (``token_usage`` / ``cache_stats`` / ``breakdown`` /
    ``filters``) lives alongside for newer consumers.
    """
    from app.models.call_log import LLMCallLog
    from app.services.semantic_cache import SemanticCache

    conds: list = []
    if project_id:
        try:
            conds.append(LLMCallLog.project_id == UUID(project_id))
        except ValueError:
            conds.append(LLMCallLog.project_id == project_id)
    cutoff = _parse_since(since)
    if cutoff is not None:
        conds.append(LLMCallLog.created_at >= cutoff)
    if task_type:
        conds.append(LLMCallLog.task_type == task_type)

    where = and_(*conds) if conds else None

    base = select(
        func.coalesce(func.sum(LLMCallLog.input_tokens), 0).label("in_tok"),
        func.coalesce(func.sum(LLMCallLog.output_tokens), 0).label("out_tok"),
        func.count(LLMCallLog.id).label("calls"),
    )
    if where is not None:
        base = base.where(where)
    row = (await db.execute(base)).one()
    in_tok = int(row.in_tok or 0)
    out_tok = int(row.out_tok or 0)
    total = in_tok + out_tok

    breakdown: list[dict[str, Any]] = []
    if group_by in ("task_type", "tier", "model"):
        col = {
            "task_type": LLMCallLog.task_type,
            "tier": LLMCallLog.tier_used,
            "model": LLMCallLog.model,
        }[group_by]
        gq = select(
            col.label("key"),
            func.coalesce(func.sum(LLMCallLog.input_tokens), 0).label("in_tok"),
            func.coalesce(func.sum(LLMCallLog.output_tokens), 0).label("out_tok"),
            func.count(LLMCallLog.id).label("calls"),
        ).group_by(col).order_by(func.sum(LLMCallLog.input_tokens + LLMCallLog.output_tokens).desc())
        if where is not None:
            gq = gq.where(where)
        gq = gq.limit(20)
        for r in (await db.execute(gq)).all():
            breakdown.append({
                "key": r.key or "(unknown)",
                "input_tokens": int(r.in_tok or 0),
                "output_tokens": int(r.out_tok or 0),
                "total_tokens": int((r.in_tok or 0) + (r.out_tok or 0)),
                "calls": int(r.calls or 0),
            })

    cache_stats = SemanticCache().get_stats()
    cache_hits = int(cache_stats.get("hits", 0) or 0)
    cache_misses = int(cache_stats.get("misses", 0) or 0)
    cache_total = cache_hits + cache_misses
    cache_hit_rate = (cache_hits / cache_total) if cache_total > 0 else 0.0

    return {
        # Backwards-compatible flat keys (used by frontend TokenDashboard)
        "totalInputTokens": in_tok,
        "totalOutputTokens": out_tok,
        "totalTokens": total,
        "cacheHits": cache_hits,
        "cacheMisses": cache_misses,
        "cacheHitRate": cache_hit_rate,
        # Richer structured payload
        "token_usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": total,
            "calls": int(row.calls or 0),
        },
        "cache_stats": cache_stats,
        "breakdown": breakdown,
        "filters": {
            "project_id": project_id,
            "since": since,
            "task_type": task_type,
            "group_by": group_by,
        },
    }
